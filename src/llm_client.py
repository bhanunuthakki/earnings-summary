"""
src/llm_client.py
-----------------
LLM client for the earnings-summary pipeline. Per-purpose prompt-bearing
generators (generate_summary, generate_thesis_update, generate_bear_case,
classify_intake_document, …) and the prompt-rendering constants they share.

The runtime plumbing — Claude CLI subprocess wiring, Gemini fallback policy,
ledger writes, anchor block builders — was extracted to the ``src/llm/``
subpackage during the split. Public names are re-exported below so every
caller in ``execution/``, ``src/compute/``, ``src/report/sections/``, etc.
keeps working with the existing ``from llm_client import X`` pattern.

Architecture after the split:
  src/llm/anchors.py  — load_thesis_anchor / load_bear_anchor / compose
  src/llm/fallback.py — Gemini fallback (try_gemini_fallback) + policy
  src/llm/cli.py      — Claude CLI subprocess + call_llm + budget enforcement
  src/llm/ledger.py   — best-effort llm_calls ledger writes

Setup (one-time, user action required):
1. Install Claude Code CLI: see https://code.claude.com/docs/en/setup
2. Set ``ANTHROPIC_API_KEY`` in the shell / ``.env``, OR run ``claude auth login``
   for the subscription path. Either works.
3. (Optional but recommended) Add ``GEMINI_API_KEY`` to ``.env`` to enable the
   fallback path. Without it, Claude CLI failures surface as hard errors. See
   .env.example.
"""

from __future__ import annotations

import json
import logging
import re

# `subprocess` is intentionally imported here (and not used directly in this
# module after the split) because the existing budget integration test
# monkeypatches `llm_client.subprocess.run`. Removing the import breaks that
# patch surface; keep the import bound to the module attribute.
import subprocess  # noqa: F401  # pyright: ignore[reportUnusedImport]
from datetime import date
from pathlib import Path
from typing import cast

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Re-exports from the src/llm/ submodules
# ---------------------------------------------------------------------------
# Every public name the original src/llm_client.py exposed is re-exported here
# so all ~30 import sites in execution/, src/compute/, src/report/sections/,
# src/report/renderers/, src/bear_case_grader, src/decision_extractor,
# src/intake, src/parser, src/synthesis_lenses, src/table_extractors/* and
# the test suite continue to work without modification.
#
# The `from X import Y as Y` pattern signals intentional re-export so neither
# ruff nor pyright flags these as unused imports. For the four legacy private
# names that have been intentionally aliased (the `_gemini_fallback_disabled`,
# `_try_gemini_fallback`, `_try_gemini_fallback_logged`, `_record_to_ledger`
# names that pre-split callers may import) the rename forces an explicit
# unused-import suppression.
#
# Note: `_setup_verified` and `_claude_cli_path` are NOT re-exports — they
# stay defined as live module-level globals below because the existing
# budget-integration test monkeypatches them via `llm_client.<name>` and the
# patch must hit the same storage that cli.py's late-import reads.
from llm.anchors import (
    ANCHOR_BLOCK_CHAR_CAP as ANCHOR_BLOCK_CHAR_CAP,
)
from llm.anchors import (
    IR_ANCHOR_CHAR_CAP as IR_ANCHOR_CHAR_CAP,
)
from llm.anchors import (
    PRIORS_ANCHOR_CHAR_CAP as PRIORS_ANCHOR_CHAR_CAP,
)
from llm.anchors import (
    compose_anchor_block as compose_anchor_block,
)
from llm.anchors import (
    load_bear_anchor as load_bear_anchor,
)
from llm.anchors import (
    load_ir_anchor as load_ir_anchor,
)
from llm.anchors import (
    load_priors_anchor as load_priors_anchor,
)
from llm.anchors import (
    load_thesis_anchor as load_thesis_anchor,
)
from llm.cli import (
    CLAUDE_WEB_TIMEOUT_SECONDS as CLAUDE_WEB_TIMEOUT_SECONDS,
)
from llm.cli import (
    CLAUDE_WEB_TOOLS as CLAUDE_WEB_TOOLS,
)
from llm.cli import (
    DEFAULT_MODEL as DEFAULT_MODEL,
)
from llm.cli import (
    DEFAULT_TIMEOUT_SECONDS as DEFAULT_TIMEOUT_SECONDS,
)
from llm.cli import (
    FAST_CLASSIFIER_MODEL as FAST_CLASSIFIER_MODEL,
)
from llm.cli import (
    LLM_MODELS as LLM_MODELS,
)
from llm.cli import (
    LLMBudgetExceeded as LLMBudgetExceeded,
)
from llm.cli import (
    LLMSetupError as LLMSetupError,
)
from llm.cli import (
    _call_claude as _call_claude,  # pyright: ignore[reportPrivateUsage]
)
from llm.cli import (
    _enforce_budget_pre_call as _enforce_budget_pre_call,  # pyright: ignore[reportPrivateUsage]
)
from llm.cli import (
    _model_for as _model_for,  # pyright: ignore[reportPrivateUsage]
)
from llm.cli import (
    _verify_setup_once as _verify_setup_once,  # pyright: ignore[reportPrivateUsage]
)
from llm.cli import (
    call_llm as call_llm,
)
from llm.cli import (
    call_llm_with_web as call_llm_with_web,
)
from llm.cli import (
    is_hard_stop as is_hard_stop,
)
from llm.fallback import (
    GEMINI_FALLBACK_MODEL as GEMINI_FALLBACK_MODEL,
)
from llm.fallback import (
    is_fallback_disabled as _gemini_fallback_disabled,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)
from llm.fallback import (
    try_gemini_fallback as _try_gemini_fallback,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)
from llm.ledger import (
    fallback_call_logged as _try_gemini_fallback_logged,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)
from llm.ledger import (
    record_llm_call as _record_to_ledger,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)
from llm.style import (
    NUMBER_FORMATTING_BLOCK as NUMBER_FORMATTING_BLOCK,
)

# Load .env at module init so GEMINI_API_KEY is available without callers having
# to import dotenv themselves. Silent no-op if .env doesn't exist. Every existing
# import path (`from llm_client import ...`) still triggers this, preserving the
# pre-split behavior where fallback.py / cli.py find env vars populated. Runs
# AFTER the re-exports above because those don't actually read env vars at
# import time — env reads happen lazily inside functions.
load_dotenv()

# Staleness threshold (days). When the most-recent evidence in the corpus is
# older than this vs. the report date, the tracker switches to STALE-CORPUS
# mode (different scorecard columns + conviction cap).
STALE_CORPUS_THRESHOLD_DAYS = 120

# Schema fields stripped from the LLM prompt — they are audit-trail metadata
# meant for the human reviewer (why a schema was edited, when it was last
# revised) and bloat the prompt without aiding the analysis. Centralized so
# both passes apply the same redaction.
SCHEMA_LLM_REDACT_FIELDS: frozenset[str] = frozenset(
    {
        "thesis_status_note",
        "schema_revision_notes",
        "last_updated",
    }
)

# Markdown JSON-fence stripper — `claude -p` occasionally wraps structured
# JSON responses in ```json ... ``` fences even when asked not to. Used by
# functions that demand strict JSON output (classify_intake_document, etc.).
JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

# Intake classifier prompt budget — keep document excerpts bounded so a
# single oversized PDF doesn't blow the prompt. 6 KB matches main's prior
# Gemini-Flash budget; well under any Claude model's context.
INTAKE_TEXT_BUDGET = 6000

log = logging.getLogger(__name__)


# Claude CLI path resolution state. `_setup_verified` / `_claude_cli_path`
# INTENTIONALLY live on this module so the existing test monkeypatch surface
# keeps working without test changes:
#
#     monkeypatch.setattr(llm_client, "_setup_verified", True)
#     monkeypatch.setattr(llm_client, "_claude_cli_path", r"C:\fake\claude.cmd")
#
# cli.py's _verify_setup_once and _call_claude reach into this module via a
# late ``import llm_client`` to read/write these globals — that's why the
# re-exports above name _verify_setup_once but the state stays here.
_setup_verified: bool = False
_claude_cli_path: str | None = None


# ---------------------------------------------------------------------------
# Prompt-bearing functions (signatures preserved — callers unchanged)
# ---------------------------------------------------------------------------


def _build_pairwise_analysis_prompt(
    prev_summary, curr_summary, anchor_block: str = ""
) -> str:
    """Compose the SayDo pairwise prompt without issuing the LLM call.

    Extracted so the synchronous (`generate_pairwise_analysis`) and batch
    (`src/llm/batch.py`) paths can share the same prompt body. Two paths
    must produce byte-identical prompts for the batched output to be
    drop-in compatible with the synchronous .tmp file format.
    """
    prev_q_str = f"{prev_summary['quarter']} {prev_summary['year']}"
    curr_q_str = f"{curr_summary['quarter']} {curr_summary['year']}"

    return f"""
    You are a Strategic Management Consultant and Senior Equity Analyst.
    **Task:** Perform a strict "Say-Do" analysis comparing the **Outlook/Guidance** from the Previous Quarter ({prev_q_str}) against the **Actual Results** reported in the Current Quarter ({curr_q_str}).

    **Hard rules — non-negotiable:**
    - Every numeric figure, dated event, and management quote must be traceable to the input summaries below. If something is not present, write `[not disclosed]`. Do not invent, infer, or back-fill from prior knowledge of this company.
    - Verbatim quotes belong in quotation marks with a source tag like `[Source: {prev_q_str} prepared remarks]` or `[Source: {curr_q_str} Q&A]`.
    - The Attribution call (Execution vs. Exogenous) is a judgment, so it MUST go through the adversarial loop below — no shortcut to a verdict.
    - **Bullet ordering:** Within EVERY section that has bullets (Say, Do, Gap Analysis), order bullets by THESIS IMPACT — most thesis-relevant first, immaterial line items (FX, share count, depreciation timing, tax rate) last. Bullets tied to a TIER-1 KPI from the THESIS ANCHOR (when provided) rank above all others, regardless of magnitude. If you would include a bullet that isn't thesis-relevant AT ALL, drop it instead of relegating it to the bottom.

    {NUMBER_FORMATTING_BLOCK}
    {anchor_block}**Input Data:**
    1.  **Previous Quarter ({prev_q_str}) Summary:**
        {prev_summary["text"]}

    2.  **Current Quarter ({curr_q_str}) Summary:**
        {curr_summary["text"]}

    **Output Format (Strict Markdown):**
    ## Analysis: {prev_q_str} vs {curr_q_str}

    ### 1. Say (The Promise — from {prev_q_str})
    *   **Guidance (quantitative):** [Specific numbers/targets, with quotes + source tags. Use `[not disclosed]` if absent.]
    *   **Strategy (qualitative):** [Key initiatives promised, quoted with source tags.]

    ### 2. Do (The Reality — reported in {curr_q_str})
    *   **Performance:** [Actuals with quotes + source tags.]
    *   **Gap Analysis:** [Specific variances vs. the Say above. State each gap as `metric: guided X → actual Y (delta Z%)`.]

    ### 3. Analyst Verdict
    *   **Performance Rating:** **MET** / **MISSED** / **EXCEEDED** (choose one — must be defensible from §2 numbers)

    ### 4. Adversarial Loop — Attribution (Execution vs. Exogenous)
    *   **Primary Thesis:** [Best read of whether the gap is Execution or Exogenous, with the strongest supporting evidence and source tags.]
    *   **Strongest Counter:** [The most credible alternative read of the same prints. Avoid generic "macro could be different" — name a specific contradicting datapoint, mix effect, or management-credibility caveat from the inputs.]
    *   **Resolution:** [Reconcile the two sides.] — **Net Conviction:** High / Medium / Low. **Observable that would flip this verdict:** [specific datapoint to watch next quarter].
    *   **Sensitivity:** [If the primary read is wrong by ±X% on the key variable, what changes about the verdict or thesis impact?]

    ### 5. Thesis Impact
    *   **Structural vs temporary:** [Must follow from §4, not asserted independently.]
    *   **Concrete delta:** [Translate the verdict into a quantified thesis impact: e.g., "raises NPV/share by ~$30 if Cloud op margin expansion sustains 200bps/q for 4 more quarters" / "compresses 2026 FCF estimate by ~$8B if capex re-rates above $185B". Show the working chain; the reader should be able to replicate.]
    *   **Tier-1 KPI implication:** [Reference the THESIS ANCHOR KPIs above by name. For each tier-1 KPI the print materially moves: state the KPI by its exact anchor name + direction (faster / slower / sideways) + how this print changes the distance to its break condition. If no anchor was provided, fall back to "tier-1 KPIs unavailable" rather than inventing KPI names.]
    *   **Bear-case engagement:** [If a BEAR-CASE ANCHOR was provided above, explicitly state which named failure mode this print confirms, refutes, or leaves unchanged. Cite the failure-mode hypothesis verbatim from the anchor so the reader can map the connection. If no bear anchor, write "no prior bear-case anchor on file" and skip — do not fabricate.]
    """


def generate_pairwise_analysis(prev_summary, curr_summary, anchor_block: str = "", ticker: str | None = None):
    """
    Generates a specific "Say-Do" analysis comparing two sequential quarters.

    ``anchor_block`` is an optional pre-formatted markdown block (typically
    from ``compose_anchor_block(load_thesis_anchor(...), load_bear_anchor(...))``)
    that anchors the §5 Thesis Impact section against the analyst's actual
    tier-1 KPIs and named bear-case failure modes. Empty string → no anchor
    block injected (back-compat for watchlist tickers).
    """
    prompt = _build_pairwise_analysis_prompt(
        prev_summary, curr_summary, anchor_block=anchor_block
    )
    prev_q_str = f"{prev_summary['quarter']} {prev_summary['year']}"
    curr_q_str = f"{curr_summary['quarter']} {curr_summary['year']}"
    try:
        return call_llm(prompt, purpose="pairwise_analysis", ticker=ticker)
    except Exception as e:
        log.error(f"Error generating pairwise analysis: {e}")
        return f"Could not generate analysis for {prev_q_str} -> {curr_q_str}."


def generate_summary(text: str, anchor_block: str = "", ticker: str | None = None) -> str:
    """
    Generates an analyst-grade prose summary of an earnings transcript.

    Output is a tight markdown writeup — opens with the analytical takeaway,
    surfaces what's accelerating / decelerating / structurally changing,
    flags management spin vs reality, and forecasts what the next quarter
    looks like given this print. Replaces the templated bullets+tables
    format that produced bureaucratic checklists.

    ``anchor_block`` is an optional markdown block carrying the analyst's
    thesis + tier-1 KPIs + named bear-case failure modes. When provided,
    the opening analytical takeaway and the "Next-quarter setup" section
    are explicitly framed against the anchor. Empty string → no anchor
    (back-compat for non-tracked tickers; the per-quarter narrative is
    still useful in isolation).
    """
    prompt = f"""
    You are writing the per-quarter earnings note for an analyst-grade
    research memo. The reader already has the numbers from the workspace
    renderer (a separate FMP-driven table renders below this writeup) —
    your job is the INTERPRETATION, not the data dump.

    {anchor_block}

    BAR: this should read like a senior buy-side analyst's quarterly note,
    not a Yahoo Finance recap. Think *Stock Market Nerd* per-quarter
    debriefs: opinion-bearing, specific, willing to call out spin, and
    explicitly forward-looking.

    EXPLICITLY FORBIDDEN — these earn an automatic rewrite:
    - Templated section headers like "## 1. Executive Summary" / "## 2.
      Financial Highlights" — write FLOWING PROSE with at most 3-4 H3
      subheads of your own choosing, not a checklist
    - Re-listing the headline financials (Revenue / EPS / Op Margin) — the
      workspace renders these from FMP data adjacent to your writeup
    - "Key Drivers: [text analysis of what drove the numbers]" generic
      paraphrasing — be specific about which lever moved
    - "Strategic Initiatives:" bucket — name a specific initiative, frame
      it analytically, or omit
    - Listing every product launch — only mention launches that move the
      thesis
    - Restating analyst Q&A topic by topic — the workspace shows the full
      parsed Q&A roster separately; pull only the Q&A moments that REVEAL
      something (management dodge, surprising disclosure, contentious
      pushback)

    {NUMBER_FORMATTING_BLOCK}

    OUTPUT FORMAT (markdown, no front-matter, no title page):

    Open with a 1-paragraph **analytical takeaway** (NOT a recap): what is
    the single most important thing this quarter, framed as it bears on
    the thesis? Lead with the verdict, then the reasoning. ~3-5 sentences.
    If a THESIS ANCHOR is provided above, the takeaway MUST name at least
    one tier-1 KPI from the anchor and state how this print moves its
    distance to break. If a BEAR-CASE ANCHOR is provided, the takeaway
    must state whether this print confirms or refutes one of the named
    failure modes (cite the failure-mode hypothesis verbatim).

    Then 3-5 H3-led prose paragraphs (use your own headers — don't pick
    from a template). Suggested directions to cover (pick what's relevant,
    skip what's not):

      ### What accelerated
      Which lines / KPIs broke trend vs prior quarter? Quantify deltas
      against the prior 2-4 quarters, not just YoY. Tie each to the
      underlying driver where management explained it.

      ### What decelerated or hit headwinds
      Same treatment for negative deltas. Distinguish secular vs cyclical
      vs one-off. If management's framing differs from the print, say so.

      ### What changed structurally
      New disclosures, mix shifts, segment redefinitions, guidance-shape
      changes. Things that change the modeling baseline, not just the
      print.

      ### What management is / isn't talking about
      Where did management lean in (and why)? What got conspicuously
      light coverage? Call out specific Q&A dodges or topic shifts vs
      the prior call.

      ### Next-quarter setup
      Concrete: what should we expect in the next print given THIS
      quarter's signals? Frame as a 1-quarter-out check on the thesis.
      When a THESIS ANCHOR is provided above, list the specific tier-1
      KPI values to watch (by their anchor names) and what reading
      would confirm or break the thesis next quarter.

    Then a final **Quotes worth keeping** subsection (optional, omit if
    nothing earns its place) — 2-4 verbatim quotes from the transcript
    that EARN inclusion (i.e., couldn't be paraphrased without losing
    signal). Format each as:
      > "verbatim quote text"
      > — Speaker Name · role

    Rules:
    - No invented numbers. Every quantitative claim must be grounded in
      the transcript or derivable from numbers IN the transcript.
    - Verbatim quotes belong in blockquote format above. Inline
      paraphrasing should not use quotation marks.
    - Skip sections that don't have signal — better a tight 4-paragraph
      note than 6 paragraphs padded with filler.
    - The writeup should be 400-900 words total. Anything shorter is
      probably under-developed; anything longer is probably padded.

    Transcript:
    """
    try:
        return call_llm(prompt + text, purpose="transcript_summary", ticker=ticker)
    except Exception as e:
        log.error(f"CRITICAL ERROR: Summary generation failed: {e}")
        raise


def generate_press_release_summary(text: str, ticker: str | None = None) -> str:
    """
    Generates a structured summary from an earnings press release.
    Press releases are financial-forward — emphasize the numbers table and guidance.
    """
    prompt = f"""
You are an expert financial analyst. Summarize the following earnings press release.
This is sourced from the company's IR website, so it is the official financial release — be precise.

**STRICT CONSTRAINT:** Do not provide conversational filler. Start your response immediately with the Report Title.

{NUMBER_FORMATTING_BLOCK}

**Output Format (Strict Markdown):**

# Press Release Summary: [Company Ticker] [Quarter] [Year]

## 1. Headline Results
| Metric | Reported | Guidance/Consensus | Beat/Miss |
| :--- | :--- | :--- | :--- |
| **Revenue** | [Value] | [Value if available] | [Result] |
| **EPS (GAAP)** | [Value] | [Value if available] | [Result] |
| **EPS (Non-GAAP)** | [Value] | [Value if available] | [Result] |
| **Operating Income** | [Value] | [Value if available] | [Result] |
| **Free Cash Flow** | [Value if disclosed] | N/A | N/A |

## 2. Key Business Metrics
[List 3–6 company-specific KPIs disclosed in the release (e.g. DAUs, GMV, RPO, cloud revenue, etc.)]

## 3. Guidance (Next Quarter & Full Year)
| Metric | Next Quarter | Full Year |
| :--- | :--- | :--- |
| **Revenue** | [Range] | [Range] |
| **EPS** | [Range] | [Range] |
| **Other** | [Any other metrics guided] | |

## 4. Capital Allocation
[Buybacks, dividends, debt changes disclosed in this release]

Press Release:
"""
    try:
        return call_llm(prompt + text, purpose="press_release_summary", ticker=ticker)
    except Exception as e:
        log.error(f"CRITICAL ERROR: Press release summary generation failed: {e}")
        raise


def generate_presentation_brief(text: str, ticker: str | None = None) -> str:
    """
    Generates a strategic brief from an earnings presentation slide deck.
    Presentations are typically 20–40 pages of slides; extract the key strategic narrative.
    """
    prompt = f"""
You are a senior equity research analyst. The following text was extracted from an earnings presentation slide deck.
Extract the key strategic narrative — what story is management telling investors?

**STRICT CONSTRAINT:** Do not provide conversational filler. Start your response immediately with the Report Title.

{NUMBER_FORMATTING_BLOCK}

**Output Format (Strict Markdown):**

# Presentation Brief: [Company Ticker] [Quarter] [Year]

## 1. Management Narrative
[2–3 sentences on the central investor story management is presenting this quarter]

## 2. Highlighted Metrics & Charts
[List key data points, KPIs, or charts that management chose to prominently feature — these signal what they want investors to focus on]

## 3. Strategic Initiatives Featured
[New products, partnerships, market expansions, or strategic pivots highlighted in the deck]

## 4. Forward-Looking Slides
[Any slides about market opportunity, TAM, roadmap, or multi-year targets]

## 5. Analyst Watchpoints
[What is management NOT showing or downplaying? Notable omissions or changed slide topics vs. prior quarters if detectable]

Presentation Text:
"""
    try:
        return call_llm(prompt + text, purpose="presentation_brief", ticker=ticker)
    except Exception as e:
        log.error(f"CRITICAL ERROR: Presentation brief generation failed: {e}")
        raise


# ---------------------------------------------------------------------------
# Shared building blocks for the thesis-tracker prompt
# ---------------------------------------------------------------------------

# Per-document character cap when assembling quarter context. Keeps the prompt
# bounded while preserving enough text for KPI-value extraction and quote pulls.
PER_DOC_CONTEXT_CAP_CHARS = 3000

# Hard rules and Adversarial Loop format are duplicated across both passes so
# each pass is self-grounded. This block is the single source of truth.
_HARD_RULES_BLOCK = """**Hard rules — non-negotiable:**
1. The Available Evidence above is the source of truth. Do NOT introduce numbers, dates, products, or events that are not present in it. Use `[not disclosed]` for any cell where the value is not in the evidence — never guess, never back-fill from prior knowledge of the issuer.
2. Every numeric KPI value, dated event, and management quote must carry an inline source tag of the form `[Source: <doc type>, <Q# YYYY>, <speaker or section>]`. Tag at the point of claim, including inside table cells.
3. The three judgment surfaces — Thesis Status, Say-Do, and Valuation-Trigger Stress — MUST each include a fully populated Adversarial Loop. A surface that lacks a credible Strongest Counter is under-examined and should be flagged as such with Net Conviction = Low rather than papered over.
4. **Inferred figures audit-trail:** any value you computed yourself rather than reading directly from a source (e.g., Q4 standalone derived as FY minus 9M; ex-items decomposition; YoY delta) MUST carry an audit-trail source tag of the form `[Source: implied = <FY src> minus <9M src>]` or `[Source: implied = <calculation>]`. Use the doc_type marker `[implied]` so inferred figures are searchable separately from primary citations. The reader must be able to reproduce your inference.
5. **Ex-items decomposition required:** for any margin / EPS / FCF / operating-income cell where the source document discloses a one-off (tax credit, restructuring charge, gain on sale, settlement, impairment, provision reversal), report as `headline X% / underlying Y% (excluding $Zm <item> [Source: ...])`. The headline number alone misleads run-rate analysis. If no one-offs are disclosed for a cell, no decomposition is needed.
6. **Methodology consistency:** when comparing the same metric across two periods, verify methodology consistency. If the issuer disclosed a methodology change, footnote restatement, or scope expansion (different geographies, different revenue recognition, different segment definition, managerial-P&L introduction), flag the comparability gap AT the comparison cell — not buried in Analyst Notes. State the prior-method value, new-method value, and approximate delta attributable to methodology vs. underlying.
7. **Prior-period guidance reference for Say-Do:** when the corpus contains the immediately-prior quarter, treat its Outlook/Guidance section as the source of "guided X" values; treat the latest-quarter's printed actuals as "actual Y." When the corpus contains only the latest quarter, Say-Do can only be evaluated if THIS quarter's docs reference prior guidance ranges. If neither condition holds, Say-Do is un-evaluable — state this explicitly and cap Say-Do conviction at Low. Do NOT fall back to trusting management's own self-attestation phrases like "we exceeded guidance across the board."
"""

_PRINCIPLES_BLOCK = """**Investment principles — frame all verdicts and recommendations through these:**

1. **One-page thesis test.** A position only earns capital if there's a coherent paragraph
   answering: (a) what the company does, (b) why the market is mispricing it, (c) what
   specifically catalyzes the re-rating, (d) when. If the report can't generate that
   paragraph from the evidence, the verdict skews toward CUT, not HOLD.
2. **Killer variables.** Identify the 2-3 fundamental drivers that actually move the
   outcome for THIS business (not generic "macro / rates / sentiment"). Frame KPI
   verdicts and Open Questions around those, not the long tail.
3. **Invalidation triggers — fundamental, not price.** Break conditions are about
   business reality (revenue growth thresholds, competitor launches, regulatory rulings),
   never about stock price drawdowns. A 20% price decline is not, by itself, a sell signal.
4. **Sizing by conviction.** Recommendations should be tiered:
     - High conviction (clean thesis + low ambiguity + observable catalysts): up to ~8-10%
     - Standard (thesis intact, some ambiguity): ~3-5%
     - Speculative (asymmetric option, broken thesis with optionality, early stage): ~1-2%
   The Sizing call must reference Net Conviction from the Adversarial Loops, not feel.
5. **Time horizon.** A fundamental thesis is years, not months. Pre-commit to N quarters
   of holding unless an invalidation trigger fires. State the horizon explicitly.
6. **Sell discipline.** Sells are justified by exactly one of: (a) thesis fully realized
   (target valuation hit / re-rating happened), (b) a specific named invalidation trigger
   fired, (c) explicit IRR comparison shows a better opportunity. NOT: bad week, boredom,
   tax-loss harvesting at the cost of the thesis. Reflect this in the verdict framing.
"""


_ADVERSARIAL_LOOP_FORMAT_BLOCK = """**Adversarial Loop format (use these exact field names):**
- **Primary Thesis:** the asserted reading + strongest supporting evidence (with source tags)
- **Strongest Counter:** the most credible name-specific challenge — alternative read, contradicting datapoint, mix/composition effect, management-credibility caveat. Reject generic macro hand-waving.
- **Resolution:** how the two sides reconcile — **Net Conviction: High / Medium / Low**. State the specific observable that would flip the verdict next period.
- **Sensitivity:** quantified impact if the primary read is wrong by ±X% on the key variable.
"""


def _compute_staleness(
    report_date: str, corpus_latest_date: str | None
) -> tuple[int, bool, str, str]:
    """
    Returns (staleness_days, is_stale, staleness_line, staleness_directive).
    Centralized so both passes apply the same staleness regime.
    """
    if corpus_latest_date is None:
        return (
            0,
            False,
            f"Corpus staleness: unknown (no corpus_latest_date provided). Report date {report_date}.",
            "Corpus is current. Standard scorecard format applies.",
        )

    report_dt = date.fromisoformat(report_date)
    latest_dt = date.fromisoformat(corpus_latest_date)
    staleness_days = (report_dt - latest_dt).days
    is_stale = staleness_days > STALE_CORPUS_THRESHOLD_DAYS
    line = f"Corpus staleness: {staleness_days} days (latest evidence {corpus_latest_date}, report date {report_date})"

    if is_stale:
        directive = f"""**STALE-CORPUS MODE** (staleness {staleness_days}d > {STALE_CORPUS_THRESHOLD_DAYS}d threshold). Apply these adaptations:
1. Add a CORPUS STALENESS DISCLAIMER as the first content under the title, naming the gap and what it means for verdict precision.
2. In the Tier-1 KPI Scorecard, REPLACE the 'vs. Break Threshold' column with 'vs. Latest Disclosed Forward Target' — compare current value to management's own most-recent forward commitment, not the schema's quantitative break (which assumes quarterly cadence the corpus does not provide).
3. Add a 'Staleness Adjustment' column on the scorecard noting an explicit ±X% uncertainty band reflecting unobserved drift over the staleness period.
4. Cap Net Conviction across all three Adversarial Loops at Low UNLESS the report explicitly justifies a higher conviction with the specific in-corpus evidence that supports it. State the cap reasoning in the Thesis Status loop's Resolution line."""
    else:
        directive = "Corpus is current. Standard scorecard format applies."

    return staleness_days, is_stale, line, directive


def _serialize_schema_for_llm(schema: dict) -> str:
    """
    Render the holdings schema as JSON for inclusion in an LLM prompt, with
    audit-trail fields (per SCHEMA_LLM_REDACT_FIELDS) removed. Those fields
    document why/when the schema was edited — useful for the human reviewer,
    noise for the model. Stripping them keeps prompt budget on the KPI
    definitions and break conditions that drive the analysis.
    """
    redacted = {k: v for k, v in schema.items() if k not in SCHEMA_LLM_REDACT_FIELDS}
    return json.dumps(redacted, indent=2)


def _format_quarter_context(quarters: list[dict]) -> str:
    """Render the chronological quarter blocks consumed by both passes."""
    blocks = []
    for q in quarters:
        block = f"\n### {q['quarter']} {q['year']}\n"
        for doc_type, text in q["summaries"].items():
            label = {
                "transcript": "Transcript Summary",
                "press_release": "Press Release Summary",
                "presentation": "Presentation Brief",
            }.get(doc_type, doc_type)
            block += f"\n**{label}:**\n{text[:PER_DOC_CONTEXT_CAP_CHARS]}\n"
        blocks.append(block)
    return "\n".join(blocks)


def _build_pass_a_prompt(
    ticker: str,
    schema: dict,
    quarters: list[dict],
    report_date: str,
    staleness_line: str,
    is_stale: bool,
    staleness_directive: str,
    quarters_context: str,
) -> str:
    """Pass A — evidence tables. Schema Hygiene, Tier-1 Scorecard, Key Developments, Breakers, Competitive."""
    thesis_text = _serialize_schema_for_llm(schema)
    scorecard_target_col = (
        "vs. Latest Disclosed Forward Target" if is_stale else "vs. Break Threshold"
    )
    scorecard_staleness_col = "Staleness Adjustment | " if is_stale else ""
    scorecard_staleness_sep = "--- | " if is_stale else ""
    scorecard_distance_phrase = (
        "distance to forward target (state as % or absolute gap) and ±X% uncertainty band reflecting unobserved drift"
        if is_stale
        else "distance to break condition (state as % or absolute gap)"
    )

    return f"""You are a senior fundamental equity analyst tracking a concentrated long position.

**Holding:** {ticker}
**Report date:** {report_date}
**{staleness_line}**

**Thesis & KPI Schema:**
{thesis_text}

**Available Evidence (last {len(quarters)} quarters, chronological):**
{quarters_context}

---

**Task — PASS A of 2: Evidence Tables.** Extract the data layer ONLY. Verdicts and adversarial loops are produced in a separate Pass B downstream — do NOT write a Thesis Status verdict or Say-Do assessment here. Output only the five sections below, in the order shown. The reader will see your output before any verdicts, so it must stand alone as a fact base.

**Corpus mode directive:**
{staleness_directive}

{_HARD_RULES_BLOCK}
{NUMBER_FORMATTING_BLOCK}
{_PRINCIPLES_BLOCK}
**Output Format (Strict Markdown — start directly at `## Schema Hygiene`, no preamble, no title):**

## Schema Hygiene (REQUIRED)
For each Tier-1 KPI in the schema, verify the issuer actually discloses that exact metric in the Available Evidence above.
- If a schema KPI does NOT match a disclosed metric (e.g., schema asks for "Total ARR Growth" but issuer only reports "Subscription ARR" and "Cloud ARR" separately), list it here with: (a) the unmatched KPI name, (b) closest-disclosed proxy if one exists, (c) recommended threshold revision (e.g., flow metric instead of stock; named-proxy substitution).
- If a schema break_condition uses a definition that is structurally always-true or always-false against issuer disclosure (e.g., "NPL 90d+ >8%" when issuer's stock NPL is structurally 16-18%), flag it here with the suggested re-baselining.
- Schema-mismatched KPIs in the scorecard below should be marked `[schema mismatch — see Schema Hygiene]` in the Status column and NOT scored as 🟢/🟡/🔴.

If all schema KPIs match disclosure cleanly, write "No mismatches detected." and proceed.

## Tier 1 KPI Scorecard
| KPI | Latest Value | Trend | {scorecard_target_col} | {scorecard_staleness_col}Status | Source |
| :--- | :--- | :--- | :--- | {scorecard_staleness_sep}:--- | :--- |
[For each tier_1_kpi in the schema: fill latest known value, direction (↑↓→), {scorecard_distance_phrase}, flag 🟢/🟡/🔴 (or `[schema mismatch — see Schema Hygiene]`), and inline source tag. Use `[not disclosed]` if missing.]

## Key Developments This Period
[3–5 bullet points on material changes — new products, macro shifts, competitive moves, management credibility events. Each bullet ends with a source tag.]

## Thesis Breaker Watchlist
| Breaker | Status | Source |
| :--- | :--- | :--- |
[For each thesis_breakers_qualitative if present in schema: current status — Active Risk / Monitoring / Cleared — with source tag for the supporting evidence. If schema has no qualitative breakers, write "N/A — schema has no qualitative breakers" and skip.]

## Competitive Watchlist Update
[Any material developments from the competitive_watchlist if present in schema, with source tags. Use `[not disclosed]` if the evidence does not cover a watchlist item. If schema has no competitive_watchlist, write "N/A".]
"""


def _build_pass_b_prompt(
    ticker: str,
    schema: dict,
    quarters: list[dict],
    report_date: str,
    staleness_line: str,
    staleness_directive: str,
    quarters_context: str,
    pass_a_output: str,
) -> str:
    """Pass B — verdicts & adversarial loops. Anchored on Pass A's KPI table."""
    thesis_text = _serialize_schema_for_llm(schema)

    return f"""You are a senior fundamental equity analyst tracking a concentrated long position.

**Holding:** {ticker}
**Report date:** {report_date}
**{staleness_line}**

**Thesis & KPI Schema:**
{thesis_text}

**Available Evidence (last {len(quarters)} quarters, chronological):**
{quarters_context}

---

**Pass A output (already produced — your verdicts must be anchored on the KPI values and developments listed here, not on a fresh re-read of the evidence):**

{pass_a_output}

---

**Task — PASS B of 2: Verdicts & Adversarial Loops.** The fact base is fixed in Pass A above; do NOT re-emit Schema Hygiene, the KPI Scorecard, Key Developments, Breakers, or Competitive Watchlist. Output ONLY the four sections below, in the order shown. KPI values cited in your loops must match those in Pass A's Tier-1 Scorecard exactly — if you would cite a different value, treat it as a Pass A error and flag it in Analyst Notes rather than silently correcting.

**Corpus mode directive:**
{staleness_directive}

{_HARD_RULES_BLOCK}
{NUMBER_FORMATTING_BLOCK}
{_PRINCIPLES_BLOCK}
{_ADVERSARIAL_LOOP_FORMAT_BLOCK}
**Output Format (Strict Markdown — start directly at `## Thesis Status:`, no preamble, no title):**

## Thesis Status: 🟢 INTACT / 🟡 MONITORING / 🔴 UNDER PRESSURE
[One sentence verdict, derived from the Pass A scorecard.]

### Adversarial Loop — Thesis Status (REQUIRED)
- **Primary Thesis:** ...
- **Strongest Counter:** ...
- **Resolution:** ... — Net Conviction: H / M / L. Flip-the-verdict observable: ...
- **Sensitivity:** ...

## Say-Do Assessment
**Verdict:** MET / MIXED / MISSED / N/A (un-evaluable — see Hard Rule 7) — derived from the gap analysis below.

**Gap Analysis (prior guidance → current actual):**
- [metric]: guided X [Source: prior-Q outlook section] → actual Y [Source: current-Q print] — delta Z%
- For ex-items adjustments per Hard Rule 5: report headline AND underlying.
- ...

### Adversarial Loop — Say-Do Attribution (REQUIRED)
- **Primary Thesis:** Execution vs. Exogenous read of the gaps above, with sourced evidence.
- **Strongest Counter:** ...
- **Resolution:** ... — Net Conviction: H / M / L. Observable that would flip: ...
- **Sensitivity:** ...

## Valuation-Trigger Stress
For each tier_1_kpi within ~15% of its break_condition (read distances from the Pass A scorecard above), AND any trigger that has fired this period, run the loop. If no T1 KPIs are within range, state that explicitly with the closest distance and skip to the next section. (Skip schema-mismatched KPIs entirely — they belong in Schema Hygiene above, not here.)

### [KPI name] — distance to break: [X% / absolute gap]
- **Primary Thesis:** [Is this trigger genuinely about to fire / has fired structurally?]
- **Strongest Counter:** [false-positive risk — single-print artifact, mix effect, FX, calendarization, methodology change per Hard Rule 6, etc.]
- **Resolution:** ... — Net Conviction: H / M / L. Confirm-or-clear observable: ...
- **Sensitivity:** [distance to threshold under ±X% scenarios on the input drivers]

## Open Questions for Next Quarter
[2–3 specific things to listen for / look for in next earnings — each tied to a Resolution flip-observable named above.]

## Portfolio & Thesis Fit
This section operationalizes the investment principles above into a position-management view. Be specific; refuse to write generic content.

**One-paragraph thesis** *(the discipline test — if you can't articulate this in one paragraph drawing only on this report's evidence, the position is mis-defined and the recommendation defaults to CUT/PASS):*
[≤120 words covering: what the company does, why the market is mispricing it (or has correctly priced it — say so), what specifically catalyzes a re-rating (or what would close the gap), expected horizon. No filler.]

**Killer variables (2–3, business-specific):**
- [variable 1 — the actual driver, not "macro"; e.g. "GLP-1 supply ramp + insurance coverage trajectory" not "drug demand"]
- [variable 2 — ditto]
- [variable 3 if needed]

**Invalidation triggers (fundamental, not price):**
- [trigger 1 — specific quarter-level observable, e.g. "FoA revenue growth <10% CC for 2 consecutive quarters"]
- [trigger 2]
- [trigger 3 if relevant — competitive event, regulator action, etc.]

**Sizing recommendation:**
- **Tier:** High conviction (≤8–10%) / Standard (3–5%) / Speculative (1–2%) / Avoid
- **Rationale:** [Tie this to Net Conviction from the three Adversarial Loops above. High conviction requires Net Conviction = High on Thesis Status AND no fired triggers. Speculative is the right call when Net Conviction = Low but the asymmetry is favorable; specify the asymmetry.]

**Time horizon & holding commitment:**
- **Pre-commit horizon:** [N quarters minimum, e.g. 8 quarters / 2 years]
- **Re-evaluation cadence:** every earnings + on any invalidation-trigger fire
- **What would shorten this:** [only the named invalidation triggers; explicitly NOT price-action]

**Sell trigger preview (for use later):**
- **Thesis-realized exit:** [specific scenario — target valuation, named re-rating event]
- **Thesis-broken exit:** [pointer to invalidation triggers above]
- **Better-opportunity exit:** [requires explicit IRR comparison vs an alternative; not "this looks cheaper"]

## Analyst Notes
[Any asymmetries, positioning thoughts, or thesis evolution observations. Mark any that rest on inference (vs. cited evidence) explicitly. Also note here any Pass A figure you believe is wrong, per the anchoring rule above.]
"""


def _assemble_tracker(
    ticker: str,
    report_date: str,
    is_stale: bool,
    staleness_days: int,
    corpus_latest_date: str | None,
    pass_a: str,
    pass_b: str,
) -> str:
    """Stitch title, optional staleness disclaimer, Pass B verdicts, then Pass A evidence tables."""
    lines = [f"# Micro-Thesis Tracker: {ticker} — {report_date}", ""]

    if is_stale:
        lines.extend(
            [
                "> **CORPUS STALENESS DISCLAIMER**: latest evidence in this tracker is "
                f"{corpus_latest_date} — {staleness_days} days stale vs. report date {report_date} "
                f"(threshold: {STALE_CORPUS_THRESHOLD_DAYS} days). Verdicts apply STALE-CORPUS MODE: "
                "scorecard compares vs. latest disclosed forward target rather than break thresholds, "
                "and adversarial-loop conviction is capped at Low absent explicit in-corpus justification.",
                "",
            ]
        )

    lines.append(pass_b.strip())
    lines.append("")
    lines.append(pass_a.strip())
    lines.append("")
    return "\n".join(lines)


def generate_thesis_update(
    ticker: str,
    schema: dict,
    quarters: list[dict],
    report_date: str,
    corpus_latest_date: str | None = None,
) -> str:
    """
    Generate an updated micro-thesis tracker document for a holding.

    Internally split into two sequential LLM passes to keep individual call
    output sizes under the per-call timeout:
      Pass A: Schema Hygiene, Tier-1 Scorecard, Key Developments, Breaker
              Watchlist, Competitive Watchlist Update (evidence tables)
      Pass B: Thesis Status verdict + loop, Say-Do + loop, Valuation-Trigger
              Stress + per-KPI loops, Open Questions, Analyst Notes (verdicts)
    Pass B is anchored on Pass A's KPI scorecard so values stay consistent.
    The function still returns a single Markdown tracker for callers.

    Args:
        ticker: Company ticker (e.g. "GOOG")
        schema: Holdings JSON schema from micro_thesis/holdings/<TICKER>.json
        quarters: List of {year, quarter, summaries: {doc_type: text}} dicts, chronological order
        report_date: ISO date (YYYY-MM-DD) the tracker is being generated for. Used to compute corpus staleness.
        corpus_latest_date: ISO date of the most recent evidence in the corpus.
            When provided, drives staleness detection. The caller should compute this
            from the latest period_end across `quarters`. None = staleness skipped.

    Returns:
        Markdown thesis tracker document.
    """
    staleness_days, is_stale, staleness_line, staleness_directive = _compute_staleness(
        report_date, corpus_latest_date
    )
    quarters_context = _format_quarter_context(quarters)

    pass_a_prompt = _build_pass_a_prompt(
        ticker,
        schema,
        quarters,
        report_date,
        staleness_line,
        is_stale,
        staleness_directive,
        quarters_context,
    )
    log.info({"event": "thesis_pass_start", "ticker": ticker, "pass": "A"})
    try:
        pass_a_output = call_llm(pass_a_prompt, purpose="thesis_pass_a", ticker=ticker)
    except Exception as e:
        log.error(f"CRITICAL ERROR: Thesis Pass A failed for {ticker}: {e}")
        raise
    log.info(
        {
            "event": "thesis_pass_done",
            "ticker": ticker,
            "pass": "A",
            "output_chars": len(pass_a_output),
        }
    )

    pass_b_prompt = _build_pass_b_prompt(
        ticker,
        schema,
        quarters,
        report_date,
        staleness_line,
        staleness_directive,
        quarters_context,
        pass_a_output,
    )
    log.info({"event": "thesis_pass_start", "ticker": ticker, "pass": "B"})
    try:
        pass_b_output = call_llm(pass_b_prompt, purpose="thesis_pass_b", ticker=ticker)
    except Exception as e:
        log.error(f"CRITICAL ERROR: Thesis Pass B failed for {ticker}: {e}")
        raise
    log.info(
        {
            "event": "thesis_pass_done",
            "ticker": ticker,
            "pass": "B",
            "output_chars": len(pass_b_output),
        }
    )

    return _assemble_tracker(
        ticker,
        report_date,
        is_stale,
        staleness_days,
        corpus_latest_date,
        pass_a_output,
        pass_b_output,
    )


def generate_strategic_analysis(summaries_list, ticker: str | None = None):
    """
    Generates a strategic analysis comparing performance vs expectations across quarters.
    summaries_list: List of dicts {'quarter': 'Q1', 'year': '2024', 'text': '...'}
    """
    context_str = ""
    for item in summaries_list:
        context_str += f"\n--- {item['quarter']} {item['year']} SUMMARY ---\n{item['text']}\n"

    prompt = f"""
    You are a Strategic Management Consultant for this company.

    **Goal:** Analyze the provided chronological earnings summaries to track the "Say-Do" ratio of management.
    Specifically, does the company achieve the goals and guidance it sets in one quarter when reported in the next?

    **Hard rules — non-negotiable:**
    - The summaries below are the source of truth. Every number, date, and quote must be traceable to them. Use `[not disclosed]` if a figure is absent — do not invent or back-fill from prior knowledge.
    - Quotes belong in quotation marks with a source tag like `[Source: Q3 2025 prepared remarks]`.
    - Each per-pair Verdict (Hit / Mixed / Miss) MUST go through the adversarial loop on attribution. The Executive Outlook Assessment at the top MUST be a synthesis of the per-pair loops, not an asserted opinion.

    {NUMBER_FORMATTING_BLOCK}

    **Input:** A sequence of earnings call summaries.

    **Adversarial Loop format (use these exact field names):**
    - **Primary Thesis:** the asserted attribution + sourced evidence
    - **Strongest Counter:** the most credible name-specific alternative read (not generic macro)
    - **Resolution:** reconciliation — **Net Conviction: High / Medium / Low**. Observable that would flip the verdict next period.
    - **Sensitivity:** quantified impact if the primary read is wrong by ±X%.

    **Output Structure:**

    # Strategic Performance Analysis

    ## Quarter-by-Quarter Track Record

    (Iterate through the timeline, comparing Q(N) Outlook to Q(N+1) Results)

    ### [Quarter N] Guidance vs [Quarter N+1] Reality
    *   **Expectation:** Specific guided numbers/targets from [Quarter N] with source tags. Use `[not disclosed]` if absent.
    *   **Reality:** Reported actuals in [Quarter N+1] with source tags.
    *   **Gap:** [metric: guided X → actual Y (delta Z%)] — list each material gap.
    *   **Verdict:** Hit / Miss / Mixed.
    *   **Adversarial Loop — Attribution:**
        - Primary Thesis: ...
        - Strongest Counter: ...
        - Resolution: ... — Net Conviction: H/M/L. Flip-the-verdict observable: ...
        - Sensitivity: ...

    ## Executive Outlook Assessment (synthesis)
    Roll up the per-pair loops above into a credibility view:
    - Pattern of Hits/Misses/Mixed across the period
    - Whether misses cluster on Execution or Exogenous attribution
    - Net management-credibility read with conviction (H/M/L) and the specific observable that would change the assessment

    ## Key Strategic Shifts
    Material changes in strategy/narrative over this period, each with source tag. Distinguish stated shifts (in transcripts) from inferred shifts (your reading) — label inferred items explicitly.

    **Tone:** Analytical, objective, and critical where necessary.
    """

    try:
        return call_llm(prompt + context_str, purpose="strategic_analysis", ticker=ticker)
    except Exception as e:
        log.error(f"CRITICAL ERROR: Analysis generation failed: {e}")
        raise


def identify_transcript_metadata(text_snippet):
    """
    Identifies the Company Ticker, Quarter, and Year from the transcript text.
    """
    prompt = """
    Analyze the following text from an earnings call transcript cover page or header.
    Identify the:
    1. Company Ticker (e.g., NVDA, GOOGL, MSFT).
       **IMPORTANT**: Always use the **Primary US Listing Ticker** (NYSE/NASDAQ) if available.
       - Example: For "Taiwan Semiconductor" or "2330.TW", return "TSM".
       - Example: For "Tencent" or "700.HK", return "TCEHY".
    2. Fiscal Quarter (Q1, Q2, Q3, or Q4).
    3. Fiscal Year (e.g., 2025).

    Return the result in this STRICT format:
    TICKER_QX_YYYY

    Example: NVDA_Q1_2026

    If you cannot identify the information with confidence, return "UNKNOWN".

    Text:
    """
    try:
        return call_llm(prompt + text_snippet[:2000], purpose="transcript_metadata").strip()
    except Exception as e:
        log.error(f"Error identifying metadata: {e}")
        return "UNKNOWN"


# Cap on the raw event-document text shipped to the model. Investor day decks
# routinely run 30-40 pages of dense PDF text (50KB+) — most of which is logo
# carousels, footnotes, and disclaimer boilerplate that doesn't shape the
# multi-year framework. 20KB ≈ 5-7 pages of dense management narrative, which
# is plenty for the headline messages + targets + segment deep-dives the
# event_brief schema needs.
_MAX_EVENT_TEXT_CHARS: int = 20_000


def _truncate_event_text(text: str, max_chars: int = _MAX_EVENT_TEXT_CHARS) -> str:
    """Trim event text to max_chars, marking the truncation explicitly so the
    model knows the input was bounded rather than missing content.

    Truncation strategy: take the first N-200 chars (introductory narrative
    + headline targets usually live early), then a 200-char snippet from the
    tail (which often carries Q&A or risk recap). The marker tells the model
    what was elided so it doesn't fabricate.
    """
    if len(text) <= max_chars:
        return text
    head_budget = max_chars - 250
    head = text[:head_budget].rstrip()
    tail = text[-200:].lstrip()
    return (
        f"{head}\n\n[…document truncated for prompt-size budget — "
        f"{len(text) - max_chars} characters elided between this point and "
        f"the closing 200 chars below…]\n\n{tail}"
    )


def generate_event_brief(text: str, anchor_block: str = "", ticker: str | None = None) -> str:
    """
    Generate a structured brief for a non-quarterly IR event: investor day, AGM,
    capital markets day, conference deck, M&A announcement, ad-hoc strategic update.

    Events differ from quarterly artifacts: they are usually long-horizon strategy
    discussions (3-5 year targets, capital allocation philosophy, segment deep-dives,
    M&A rationale) rather than near-term financial results. Skip period numbers unless
    they materially shape the multi-year framework.

    ``anchor_block`` (optional) injects the thesis + tier-1 KPIs so §7 (Thesis
    Read-Through) can name specific pillars rather than write generic
    "strengthens / weakens / neutral" prose.

    The raw event text is truncated to _MAX_EVENT_TEXT_CHARS before injection.
    Investor day decks can hit 30-40 pages (50KB+); most of that is layout
    chrome, disclaimers, and logo carousels rather than the multi-year
    framework the schema needs. Truncation marker tells the model the input
    was bounded rather than missing content.
    """
    bounded_text = _truncate_event_text(text)
    prompt = f"""You are a senior equity analyst summarizing an IR event document.

Events differ from quarterly artifacts: they are usually long-horizon strategy
discussions (3-5 year targets, capital allocation philosophy, segment deep-dives,
M&A rationale) rather than near-term financial results. Skip period numbers unless
they materially shape the multi-year framework.

{anchor_block}**STRICT CONSTRAINT:** Start immediately with the title. No conversational filler.

{NUMBER_FORMATTING_BLOCK}

**Output Format (Strict Markdown):**

# Event Brief: [Ticker] [Event Name] [Date]

## 1. Event Type & Context
*   **Type:** [Investor Day / AGM / Capital Markets Day / Conference / M&A announcement / Other]
*   **Setting:** [Date, location if relevant, audience]
*   **Why it matters:** [1-2 sentences — what was the management goal for the event]

## 2. Headline Strategic Messages
[3-5 bullets on the core narratives management was trying to land. Lead with what's NEW
or DIFFERENT vs. prior management framing.]

## 3. Multi-Year Targets & Frameworks
*   **Quantitative targets:** [5-year revenue/FCF/margin targets, capital deployment ranges]
*   **Time horizon:** [Stated horizon — 3yr, 5yr, "through the cycle"]
*   **Comparison to prior:** [If the company previously gave a framework, note shifts]

## 4. Capital Allocation
[Explicit framing on buybacks, dividends, M&A appetite, balance-sheet priorities, payout ratios]

## 5. Segment / Product Deep-Dives
[Material new disclosures by segment — TAM, growth drivers, unit economics, competitive positioning]

## 6. Risks & Watchpoints
*   **Acknowledged:** [What management explicitly flagged as risks]
*   **Unaddressed:** [What investors will want to ask but didn't get clear answers on]

## 7. Thesis Read-Through
[2-3 sentences on whether this strengthens, weakens, or is neutral to a long-term holder's thesis. When a THESIS ANCHOR is provided above, name the specific tier-1 KPIs or business-model rules this event moves and in what direction. Generic "strengthens long-term thesis" earns a rewrite.]

Event Document Text:
"""
    try:
        return call_llm(prompt + bounded_text, purpose="event_brief", ticker=ticker)
    except Exception as e:
        log.error(f"CRITICAL ERROR: Event brief generation failed: {e}")
        raise


_MAX_WEB_RESULTS_PER_NEWS_CALL: int = 7
_MAX_EXCERPT_CHARS_PER_NEWS_ITEM: int = 400


def generate_recent_developments(
    ticker: str,
    news_days: int = 7,
    anchor_block: str = "",
    max_web_results: int = _MAX_WEB_RESULTS_PER_NEWS_CALL,
    max_excerpt_chars: int = _MAX_EXCERPT_CHARS_PER_NEWS_ITEM,
) -> str:
    """Recent-developments brief sourced via Claude WebSearch + WebFetch.

    Routes through `call_llm_with_web` so the model can pull current news
    from Bloomberg / Reuters / CNBC / FT / WSJ / company press releases and
    cite URLs inline. On Claude failure, `call_llm_with_web` falls back to
    plain `_call_claude` (no web), then to Gemini per the standard chain.

    Used by the §8 Recent developments section. Output is markdown, with
    sources as inline links the section renderer passes through unchanged.

    ``anchor_block`` (optional) injects the thesis + bear-case anchor so the
    "rank by thesis impact" rule and the per-item implication clauses can
    actually reference the analyst's tier-1 KPIs and named bear failures
    rather than guessing.

    ``max_web_results`` / ``max_excerpt_chars`` cap the model's web budget
    explicitly: prior versions instructed "3-7 items" but never bounded
    the number of web_fetch calls or per-article quote length, so latency
    and cost varied wildly (6-50KB prompts depending on article density).
    The defaults match the audit's "max 7 results, 500-char excerpt"
    guidance — tighten via the kwargs when calling for very tight contexts.
    """
    prompt = f"""You are a senior equity analyst preparing a recent-developments
brief for {ticker} for an analyst-grade research memo. Bar: every item
must move the thesis or be tracking a specific known catalyst — pure news
recap earns an automatic rewrite.

{anchor_block}Search the web for {ticker} news from the last {news_days} days. Prioritize
Bloomberg, Reuters, CNBC, FT, WSJ, and company press releases. Skip blog
spam, opinion pieces with no new information, recapitulation of older news,
and analyst initiation reports unless they include a non-obvious data point.

WEB BUDGET (HARD CAPS — do not exceed):
- Issue AT MOST 2 web_search queries total.
- Open AT MOST {max_web_results} URLs via web_fetch across the entire call.
- For each fetched article, quote AT MOST {max_excerpt_chars} characters
  inline. Paraphrase the rest. Long verbatim quotes do not improve the
  memo and burn input tokens with no marginal value.
- If the search returns more candidates than {max_web_results}, pick the
  highest-thesis-impact subset (see ranking rules below) and discard the
  rest before fetching. Don't fetch URLs you won't cite.

RANKING + filtering rules:
- Order each section by THESIS IMPACT (highest first), NOT chronologically.
  When a THESIS ANCHOR is provided above, "thesis impact" means: which
  named tier-1 KPI does this item move, and in what direction relative to
  its break condition? Items that touch a tier-1 KPI rank above items
  that touch a tier-2 KPI; items that touch nothing in the anchor rank
  last (or are dropped).
- For each item: the gloss must explain the IMPLICATION for the investor,
  not just restate the headline. "X happened" is wrong; "X happened, which
  shortens the runway for Y by ~Z months" is right. When the anchor names
  a relevant KPI or failure mode, cite it explicitly in the implication
  clause (e.g., "tightens the GCP-margin trajectory KPI", "partially
  confirms the AI-Mode query-dilution failure mode").
- Skip items that are purely stock-price commentary, sell-side rating
  changes without a new data point, or pure recapitulation of prior news.
- Skip ANY item that's older than {news_days} days. Don't pad.

{NUMBER_FORMATTING_BLOCK}

**Output Format (Strict Markdown):**

### Material news
- **[Headline]** — [1-2 sentences: what happened AND specific implication for the thesis / valuation / KPI trajectory. Quantify the implication where the news supports it.] [Source: outlet, YYYY-MM-DD, URL]
- ... (3-7 items, ranked by thesis impact)

### Sector / regulatory context
- [optional 1-3 items: peer earnings prints, FDA / antitrust decisions affecting peers, sector ETF flows, macro shifts that hit this ticker's specific exposures. Each item must have a "why this matters for {ticker}" clause.]

### Watch this week
- [1-3 items: upcoming earnings calls (this ticker or named peers), scheduled disclosures, investor days, regulatory dockets within the next ~7 days. Format: `**Date · Event** — what to watch for`]

If no material news found in the window, write `*No material news in the last
{news_days} days.*` under "Material news" and skip the other two sections.
Do not pad with stale or low-signal items just to fill the section.
"""
    try:
        return call_llm_with_web(prompt, purpose="recent_developments", ticker=ticker)
    except Exception as e:
        log.error(f"CRITICAL ERROR: Recent-developments generation failed for {ticker}: {e}")
        raise


_NEWS_STRUCTURING_RETRY_PREAMBLE = (
    "IMPORTANT: your previous response was not a valid JSON array. Return ONLY "
    "the JSON array specified below — no markdown fences, no commentary, no prose.\n\n"
)


def _parse_news_json_array(raw: str) -> list[object] | None:
    """Parse an LLM response into a JSON array, tolerating ```json fences.

    Returns None when the text isn't a JSON array (the caller retries once, then
    degrades to []). An empty array is a valid response ("no determinable news").
    """
    stripped = raw.strip()
    if stripped.startswith("```"):
        stripped = JSON_FENCE_RE.sub("", stripped).strip()
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, list):
        return None
    return cast("list[object]", payload)


def structure_recent_news_json(
    ticker: str,
    news_days: int = 2,
    anchor_block: str = "",
    max_web_results: int = _MAX_WEB_RESULTS_PER_NEWS_CALL,
) -> list[object]:
    """WebSearch recent news for ``ticker`` and return STRUCTURED rows as a JSON
    array — the FMP-independent fallback ingester (plan §4.3).

    Each item is ``{"headline", "url", "published_at", "published_tz", "snippet",
    "source"}``. The hard rule is baked into the prompt: return UTC where
    determinable, and OMIT any item whose publication date can't be determined
    (never guess) — preserving the trigger's "degrade, never fabricate" contract
    at the source. The caller (execution/fetch_news_websearch.py) normalizes each
    item's timestamp to UTC, drops anything still missing url/headline/date, and
    maps to a NewsRow.

    Routes through ``call_llm_with_web`` with ``purpose="news_structuring"``,
    which resolves to Opus via ``LLM_MODELS``. Returns the parsed list of item
    dicts, or ``[]`` on any LLM/parse failure (one retry on a parse miss,
    mirroring the material_news trigger's discipline). The web budget is capped.
    """
    framing = anchor_block.strip()
    anchor_clause = f"{framing}\n\n" if framing else ""
    prompt = (
        f"You are sourcing recent news for a long-term investor in {ticker} and "
        f"returning it as STRUCTURED DATA (not prose).\n\n"
        f"{anchor_clause}"
        f"Search the web for {ticker} news from the last {news_days} days. "
        f"Prioritize Bloomberg, Reuters, CNBC, FT, WSJ, and company press "
        f"releases. Skip blog spam, opinion pieces with no new information, and "
        f"pure stock-price chatter.\n\n"
        f"WEB BUDGET (HARD CAPS): issue AT MOST 2 web_search queries; open AT "
        f"MOST {max_web_results} URLs via web_fetch.\n\n"
        "Return ONLY a JSON array, one object per distinct story, EXACTLY:\n"
        '[{"headline": "<title>", "url": "<canonical article url>", '
        '"published_at": "YYYY-MM-DD HH:MM:SS", "published_tz": "UTC", '
        '"snippet": "<one-sentence gloss>", "source": "<outlet, e.g. Reuters>"}]\n\n'
        "HARD RULES:\n"
        "- published_at: the publication timestamp where you can determine it, "
        "formatted 'YYYY-MM-DD HH:MM:SS' (24-hour, a space not 'T', no zone "
        "suffix). Give it in UTC and set published_tz to 'UTC'. If you only know "
        "the US/Eastern wall-clock time, return that and set published_tz to 'ET'.\n"
        "- If you CANNOT determine a publication date for a story from its "
        "source, OMIT that story entirely. Never guess or fabricate a date.\n"
        "- url must be the real article URL (it is the dedup key). Omit any item "
        "without one.\n"
        "- Output the JSON array and nothing else: no markdown fences, no prose."
    )
    for attempt, body in enumerate((prompt, _NEWS_STRUCTURING_RETRY_PREAMBLE + prompt)):
        try:
            raw = call_llm_with_web(body, purpose="news_structuring", ticker=ticker)
        except Exception as exc:
            log.warning(
                f"news_structuring web call failed for {ticker} (attempt {attempt + 1}): {exc}"
            )
            return []
        parsed = _parse_news_json_array(raw)
        if parsed is not None:
            return parsed
        log.warning(
            f"news_structuring returned non-array JSON for {ticker} (attempt {attempt + 1})"
        )
    return []


def _ticker_specific_block(md: str) -> str:
    """Wrap optional per-ticker enhancement context (see Phase 5) for the prompt.

    Empty input → empty output (no extra newlines), so the universal prompt
    shape is unchanged when no per-ticker enhancements exist for this name.
    """
    if not md.strip():
        return ""
    return f"\nTICKER-SPECIFIC CONTEXT (per-ticker research enhancements):\n{md}\n"


def _ir_anchor_prompt_block(md: str) -> str:
    """Wrap the IR-narrative anchor (with its baked-in bias-framing header)
    for inclusion in an analytical prompt.

    The caller is expected to have already invoked ``load_ir_anchor`` — the
    body already carries its own ``## IR ANCHOR (... USE WITH SKEPTICISM)``
    heading. This wrapper only handles the prefix newline + empty-case guard
    so the surrounding prompt template doesn't sprout a dangling blank line
    when the ticker has no IR-deck cache.
    """
    if not md.strip():
        return ""
    return f"\n{md}\n"


def _ts_signals_prompt_block(md: str) -> str:
    """Wrap a pre-formatted time-series signals markdown block for inclusion
    in an analytical prompt.

    The caller (a section builder) is expected to have already invoked
    ``report.sections._ts_signals.format_signals_as_prompt_block`` to
    obtain ``md`` — the body already carries its own ``## <heading>`` line.
    This wrapper only handles the prefix newline + empty-case guard so the
    surrounding prompt template doesn't sprout a dangling blank line when
    a ticker has no persisted signals.
    """
    if not md.strip():
        return ""
    return f"\n{md}\n"


def generate_bear_case(
    ticker: str,
    thesis: str,
    break_conditions: list[str],
    last_quarter_summaries: list[str],
    financials_table_md: str,
    segments_table_md: str,
    kpi_status_md: str,
    ticker_specific_md: str = "",
    ts_signals_md: str = "",
    ir_anchor_md: str = "",
    repo_root: Path | None = None,
) -> str:
    """
    Generate a structured bear case as a JSON string the caller parses.

    Schema: {failure_modes: list[FailureMode], most_underweighted: str,
    out_of_scope_flags: list[str]}.

    `repo_root`, when set, lets the prompt pre-aggregate the 12Q financial
    + segment series into a "Recent statistical patterns" block (trend
    direction + slope significance + inflection callouts). Without it the
    model has to eyeball raw tables; with it we save ~2-3K input tokens
    per call and the failure-mode hypotheses become more specific because
    the model isn't burning attention on trend detection it can read off
    the precis. Best-effort — the table block still ships unchanged so a
    DB / loader failure can't degrade the prompt.

    `ts_signals_md` is a markdown block (typically built via
    ``_ts_signals.format_signals_as_prompt_block`` over the union of every
    signal computed for the ticker) that surfaces persisted time-series
    primitives the writer flagged. The bear case wants the broadest
    coverage of disconfirming patterns, so the caller passes ALL signals
    and the model treats each line as a disconfirmation candidate to
    engage with in the failure_modes / out_of_scope_flags arrays. Empty
    string when the writer hasn't run or this ticker has no signals.
    """
    transcripts_block = "\n\n".join(
        f"### Quarter {i + 1} (oldest first)\n{s[:6000]}"
        for i, s in enumerate(last_quarter_summaries)
    )

    stats_lines: list[str] = []
    if repo_root is not None:
        try:
            from llm.anchors import _statistical_patterns_block

            stats_lines = _statistical_patterns_block(repo_root, ticker, {})
        except Exception as exc:  # never block the bear-case build
            log.debug(
                f"bear_case stats block skipped for {ticker}: "
                f"{type(exc).__name__}: {exc}"
            )
            stats_lines = []
    stats_block = (
        "\nRECENT STATISTICAL PATTERNS (computed; trust over the raw table):\n"
        + "\n".join(stats_lines)
        + "\n"
        if stats_lines
        else ""
    )

    prompt = f"""You are a senior fundamental equity analyst writing the bear
case for {ticker}. The bar is a deep-dive newsletter take, not a Yahoo
Finance risks list. You are arguing the SHORT side to a thoughtful portfolio
manager who already knows the bull case.

BAR + framing rules:
- Every failure_mode must be SPECIFIC to {ticker}'s business model. Generic
  risks ("revenue could decelerate", "macro could weaken", "competition")
  earn an automatic rewrite. Tie each risk to a named mechanic of THIS
  business — its pricing, unit economics, regulatory exposure, capex
  profile, channel concentration, switching-cost economics, etc.
- At least TWO of the failure_modes must be NON-CONSENSUS — risks that
  sell-side coverage has NOT broadly flagged or that are systematically
  underweighted by buy-side because of organizational bias, model inertia,
  or framing blind-spots. If you can't think of a non-consensus failure
  mode for this business, you haven't thought hard enough.
- Cite competitive dynamics with NAMED rivals where relevant ("OpenAI
  + Anthropic capture X% of the GenAI query budget that was previously
  Google Search... ", "AWS retains the enterprise-AI workload pipeline
  via Bedrock's distribution lead..."). Quantify where the input data
  supports it; otherwise be honest about the qualitative claim.
- Each `evidence_in_data` MUST cite a specific number or trend from the
  inputs (e.g., "FCF dropped from $24.6B Q4'25 to $5.3B Q2'25 — capex
  inflection running ahead of OCF growth"). Vague "growth is decelerating"
  doesn't qualify.
- `quantitative_impact` must do the actual math: link the failure mode to
  a specific revenue / margin / FCF / NPV-per-share delta with the
  reasoning chain shown. The reader should be able to plug your numbers
  into a model and replicate your scenario.
- Grounded ONLY in the data below. Don't fabricate. If a real risk is
  real but not derivable from these inputs, put it under
  `out_of_scope_flags` with a 1-line explanation of why it's parked.

{NUMBER_FORMATTING_BLOCK}

THESIS:
{thesis}

BREAK CONDITIONS:
{json.dumps(break_conditions, indent=2)}

LAST {len(last_quarter_summaries)}Q SUMMARIES:
{transcripts_block}

QUARTERLY FINANCIALS (12Q):
{financials_table_md}

SEGMENT TRENDS (12Q):
{segments_table_md}

KPI STATUS:
{kpi_status_md}
{stats_block}{_ticker_specific_block(ticker_specific_md)}{_ts_signals_prompt_block(ts_signals_md)}{_ir_anchor_prompt_block(ir_anchor_md)}
When the IR ANCHOR block is present above, use it to identify management's
ON-RECORD framing of the business (the TAM, growth runway, strategic
priorities, competitive positioning they choose to highlight). Failure
modes are STRONGER when they explicitly engage with this framing — point
out where management's positioning OVERSTATES, OMITS, or MISFRAMES a
reality the data suggests. The bias-framing header in the IR ANCHOR is
not decorative; treat the content with skepticism.

---

Produce a JSON object with EXACTLY these keys (no markdown, no commentary):

{{
  "failure_modes": [
    {{
      "hypothesis": "one-sentence concrete failure mode — must name a specific business-model mechanic of {ticker}",
      "evidence_in_data": "cite a specific number or trend from the inputs above (with the value AND the time period). Vague paraphrasing earns an automatic rewrite.",
      "leading_indicator": "what would confirm it in the NEXT 1-2 prints. Must be a numerical / disclosed metric, not a qualitative vibe.",
      "quantitative_impact": "do the math: link the failure mode to a specific revenue / margin / FCF / NPV-per-share delta. Show the reasoning chain so the reader can replicate or stress-test.",
      "refutation_criteria": "what management would have to disclose or demonstrate over the next 2-4Q to neutralize this thesis. Specific and falsifiable."
    }}
  ],
  "most_underweighted": "one-paragraph editorial argument: which of the failure modes above is most underweighted by sell-side / consensus, and WHY consensus is structurally blind to it (e.g., model inertia, organizational bias of legacy bull-side analysts, framing blind-spot, etc.). Don't pick the most-likely failure mode — pick the one that consensus is most-wrongly-pricing relative to its actual probability × impact.",
  "out_of_scope_flags": ["each entry: a real risk that's NOT derivable from the inputs above (regulatory, macro, technological, etc.) — with a brief reason why we're parking it. 1-3 entries max."]
}}

Provide 3 to 5 failure_modes. At least 2 must be non-consensus per the rule
above. Return strictly the JSON object — nothing else.
"""
    try:
        raw = call_llm(prompt, purpose="bear_case", ticker=ticker).strip()
        # Strip ``` fences if Claude wraps the JSON despite the instruction.
        if raw.startswith("```"):
            raw = JSON_FENCE_RE.sub("", raw).strip()
        return raw
    except Exception as e:
        log.error(f"CRITICAL ERROR: Bear case generation failed for {ticker}: {e}")
        raise


def generate_qa_topics(
    ticker: str,
    quarter_label: str,
    questions: list[dict[str, str]],
) -> str:
    """Generate short topic labels for a batch of analyst Q&A questions.

    ``questions`` is a list of ``{"id": "0", "analyst": str, "question": str}``
    dicts. Returns a JSON list ``[{"id": str, "topic": str, "tag": str}, ...]``
    where ``topic`` is a 4-7 word noun phrase and ``tag`` is one of the
    coarse business-area chips the renderer styles (INFRA, CLOUD, SEARCH,
    MARGIN, CAPEX, AGENT, LEGAL, OTHER BETS, CONSUMER, Q&A as fallback).

    Batched per quarter (one LLM call per quarter instead of one per question)
    so the cost stays in the sub-penny range for the typical 6-10 question
    transcript.
    """
    payload = json.dumps(questions, ensure_ascii=False)
    prompt = f"""You are labelling analyst-call Q&A topics for an equity research dashboard.
For each analyst question below, return a short, scannable topic phrase that
captures the substance — what's actually being asked — NOT throat-clearing or
sequence ("two for me", "one for Anat"). 4-7 words. Use noun phrases, not
sentences. Also pick a coarse business-area tag from this set:
INFRA, CLOUD, SEARCH, MARGIN, CAPEX, AGENT, LEGAL, OTHER BETS, CONSUMER, Q&A.

TICKER: {ticker}
QUARTER: {quarter_label}

QUESTIONS (JSON):
{payload}

Return ONLY a JSON array (no markdown, no commentary):

[
  {{"id": "0", "topic": "Compute allocation across internal vs Cloud", "tag": "INFRA"}},
  ...
]

The "id" must echo back the input id verbatim. The "topic" must read cleanly
on its own — no "asks about" prefix; just the topic itself.
"""
    try:
        raw = call_llm(prompt, purpose="qa_topics", ticker=ticker).strip()
        if raw.startswith("```"):
            raw = JSON_FENCE_RE.sub("", raw).strip()
        return raw
    except Exception as e:
        log.error(f"CRITICAL ERROR: Q&A topic generation failed for {ticker}: {e}")
        raise


def generate_saydo_filter(
    ticker: str,
    quarter_label: str,
    rows: list[dict[str, str]],
    anchor_block: str = "",
) -> str:
    """Pick the strategically important commitments out of a print-vs-guide table.

    ``rows`` is ``[{"id": str, "metric": str, "guide": str, "actual": str,
    "verdict": str}, ...]``. Returns a JSON list of the IDs that matter for
    the thesis — skips FX trivia, share count noise, and other below-the-line
    items. Up to 6 rows kept.

    ``anchor_block`` (optional) injects the thesis + tier-1 KPI list so the
    "matters for the thesis" judgment can be made against the analyst's
    actual KPIs rather than the model's generic prior about what matters.
    """
    payload = json.dumps(rows, ensure_ascii=False)
    prompt = f"""You are filtering an analyst's print-vs-guide commitments table
for {ticker} {quarter_label}. Most rows in these tables are noise (FX, share
count, tax rate, depreciation timing). The reader wants ONLY the commitments
that move the investment thesis — revenue trajectory, segment growth,
margin direction, capex / FCF, major product or partnership commitments,
balance-sheet structural shifts.

{anchor_block}ROWS (JSON):
{payload}

Return ONLY a JSON array of the IDs to KEEP (up to 6, ordered by importance):

["3", "1", "5"]

Skip anything that's a maintenance/macro line. Prefer rows where the verdict
is EXCEEDED or MISSED (signal) over rows that are clean MET (less signal).

When a THESIS ANCHOR is provided above, prioritize rows whose metric maps
to one of the named tier-1 KPIs or business-model rules. Rows that touch a
tier-1 KPI rank above rows that don't, even if the latter is a bigger
delta. The point is to filter for THESIS-relevant signal, not generic
beats/misses.
"""
    try:
        raw = call_llm(prompt, purpose="saydo_filter", ticker=ticker).strip()
        if raw.startswith("```"):
            raw = JSON_FENCE_RE.sub("", raw).strip()
        return raw
    except Exception as e:
        log.error(f"CRITICAL ERROR: SayDo filter failed for {ticker}: {e}")
        raise


def generate_company_description(
    ticker: str,
    profile_description: str,
    sector: str | None,
    industry: str | None,
    form_10k_text: str,
    segment_names: list[str],
    geo_names: list[str],
    fiscal_year: int | None,
    thesis_text: str = "",
    recent_earnings_md: str = "",
    recent_ir_md: str = "",
    ir_anchor_md: str = "",
) -> str:
    """Synthesize the §2 Company description as an analyst-grade business writeup.

    Inputs go beyond the 10-K — the prompt is fed the user's own thesis
    statement, the most recent earnings narrative(s), recent IR document
    condensers (`recent_ir_md`), and the raw IR-deck narrative with
    bias-framing (`ir_anchor_md`). The intent is to elicit a *thesis-anchored*
    take on the business (what makes it interesting as an INVESTMENT, where
    the moat actually is, what consensus underweights) rather than a 10-K
    paraphrase — and to force the writeup to engage skeptically with
    management's own positioning rather than parroting it.

    `recent_ir_md` and `ir_anchor_md` are deliberately distinct: the former is
    a factual condenser of recent IR events (what was disclosed); the latter
    is raw slide-deck narrative carrying its own bias-framing header so the
    LLM treats it as company spin, not ground truth.

    Returns a JSON string the caller parses. Schema:
      {
        "elevator_pitch": "1-2 sentence positioning statement",
        "business_overview": "multi-paragraph analytical take on the business",
        "revenue_model": "how the company makes money, with take-rate / unit-economics specifics",
        "segments": [{"name": "Google Services", "description": "..."}, ...],
        "geographies": [{"name": "United States", "description": "..."}, ...]
      }
    """
    segments_block = (
        "\n".join(f"- {n}" for n in segment_names) if segment_names else "(none on file)"
    )
    geos_block = "\n".join(f"- {n}" for n in geo_names) if geo_names else "(none on file)"
    thesis_block = (
        f"INVESTOR THESIS ON FILE (the analyst's own framing of this name):\n"
        f'"""\n{thesis_text.strip()}\n"""\n'
        if thesis_text.strip()
        else ""
    )
    recent_earnings_block = (
        f"RECENT EARNINGS NARRATIVE (most recent management framing — use this to "
        f"anchor the business writeup, not the 10-K boilerplate):\n"
        f'"""\n{recent_earnings_md.strip()[:12000]}\n"""\n'
        if recent_earnings_md.strip()
        else ""
    )
    recent_ir_block = (
        f"RECENT IR DOCUMENT EXCERPTS (press releases, investor day decks):\n"
        f'"""\n{recent_ir_md.strip()[:8000]}\n"""\n'
        if recent_ir_md.strip()
        else ""
    )
    # IR anchor carries its own ## IR ANCHOR (USE WITH SKEPTICISM) heading from
    # load_ir_anchor; pass it through verbatim so the bias frame stays intact.
    ir_anchor_block = (
        f"{ir_anchor_md.strip()}\n" if ir_anchor_md.strip() else ""
    )
    prompt = f"""You are writing the "Company" section of an analyst-grade
investment memo on {ticker}. Voice: a senior buy-side analyst's working
note. Not Wikipedia. Not a 10-K paraphrase.

ANCHOR YOUR WRITEUP ON THE ANALYST'S THESIS (below). Every paragraph
should advance one of the thesis pillars (value driver, moat, pressure
point, optionality) with specific numbers and named competitors.

When the IR ANCHOR block is present below, treat it as COMPANY-PROVIDED
FRAMING — material useful for understanding *how management positions the
business* but NOT as ground truth. Cite specific framing where it's
revealing ("the company describes its TAM as $X, which compares to..."),
push back where the positioning diverges from what the 10-K, segment
numbers, or competitive dynamics support, and form your own analytical
POV rather than paraphrasing the deck.

{thesis_block}{recent_earnings_block}{recent_ir_block}{ir_anchor_block}
SEGMENTS this report displays (use these EXACT names):
{segments_block}

GEOGRAPHIES this report displays (use these EXACT names):
{geos_block}

10-K segment-naming excerpts (factual grounding only — do not paraphrase):
\"\"\"
{form_10k_text.strip()[:2500] or "(none)"}
\"\"\"

Profile blurb (third-party — do not copy):
"{profile_description.strip()[:500] or "(none)"}"

Sector/Industry/Source-FY: {sector or "?"} / {industry or "?"} / {fiscal_year or "?"}

---

OUTPUT — a JSON object with EXACTLY these fields. The schema is
**component-based**: you emit analytical pieces, the renderer assembles
the final prose. Do NOT try to write a polished elevator pitch or
business overview yourself — the schema fields are deliberately structured
so the analytical voice is forced by the format, not by your prose
choices.

```json
{{
  "value_driver_phrase": "noun-phrase clause, 6-15 words, that will be CONCATENATED into the string '{ticker}: <phrase>.'. Must read as a continuation of '{ticker}: ', NOT as a standalone sentence. Must name the cash-engine mechanic AND a concrete economic anchor (margin, take rate, scale figure).",
  "central_bet": "10-20 words framing the central bull-vs-bear debate as a TESTABLE quantified hypothesis. Will be concatenated as 'The bet: <central_bet>'. Examples: 'whether GCP margin expansion absorbs the $180B+ 2026 capex before Gemini cannibalization compresses Search', 'whether ARPAC sustains 25%+ CAGR through 2028 once secured-credit saturates Brazil'.",
  "swing_variable": "Optional null-able 1-sentence on what to watch for the next 4 quarters — the single most important number that will resolve the bet. Pass null if no clean swing variable exists.",
  "paragraphs": [
    {{
      "opener": "<ONE OF: 'The cash engine is' / 'The growth optionality is' / 'The structural debate is' / 'The pressure point most analysts underweight is' / 'What is non-obvious is' / 'The competitive dynamic is' / 'The moat depends on'>",
      "body": "Rest of the paragraph, ~80-200 words. MUST cite at least one specific number from the inputs (revenue, margin, growth rate, market share). MUST name at least one specific competitor or comparable company. MUST tie back to a thesis pillar."
    }}
  ],
  "revenue_mechanics": [
    {{
      "topic": "<ONE OF: 'take_rate' / 'unit_economics' / 'capex_intensity' / 'mix_shift' / 'scale_dynamics' / 'cash_conversion'>",
      "body": "1 paragraph (~80-150 words) on the economic mechanic. Use specific numbers + comparison to peers where available. Do NOT write 'X generates revenue through Y' descriptions."
    }}
  ],
  "segments": [
    {{"name": "<exact segment name from the SEGMENTS list above>", "description": "1-2 sentences with a SPECIFIC economic mechanic (margin trajectory, growth rate, contribution to OI) + competitive position. Forbidden phrases: 'includes products such as', 'encompasses', 'generates revenue from'. Immaterial segments: 'immaterial (<X% of revenue), primarily Y, runs Z operating loss/year'."}}
  ],
  "geographies": [
    {{"name": "<exact geography name from the GEOGRAPHIES list above>", "description": "1 sentence with analytical content (concentration, growth differential, regulatory exposure). Skip generic geo-disclosure rows."}}
  ]
}}
```

Field-by-field rules:

- `value_driver_phrase`: must be a NOUN PHRASE. It will be string-
  concatenated as `f"{ticker}: {{value_driver_phrase}}."`. So "a
  search-ads cash engine ($240B run-rate)" is valid; "the company
  operates a search business" is invalid because it parses as a verb
  phrase + the assembled sentence would read awkwardly. Good examples:
  * "a search-ads quasi-monopoly funding the largest first-party AI
    distribution stack in the world (~$240B run-rate, 35%+ op margin)"
  * "the per-customer monetization arc compounding ~3x faster than
    incumbent banks at 1/10th the cost-to-serve"
  * "a semiconductor monopoly capturing ~$0.92 of every hyperscaler GPU
    dollar at 73% gross margin"

- `paragraphs`: emit 3-5 entries. Use openers from the allowed list.
  Don't reuse the same opener twice. Order them by analytical priority
  (cash engine first, then growth optionality, then debates/pressure
  points).

- `revenue_mechanics`: emit 1-3 entries. Pick the topics that actually
  matter for THIS business. If take rate doesn't apply (e.g., for a pure
  consumption-fee model), pick mix_shift or scale_dynamics instead.

- `segments`: include EVERY segment name from the list — even
  immaterial ones (with a 1-line note).

- `geographies`: only entries with analytical signal. Skip rows where
  the only thing to say is "represents Western Europe".

Return ONLY the JSON object. No markdown fence, no prose before or after.
"""
    try:
        raw = call_llm(prompt, purpose="company_description", ticker=ticker).strip()
        if raw.startswith("```"):
            raw = JSON_FENCE_RE.sub("", raw).strip()
        return raw
    except Exception as e:
        log.error(f"CRITICAL ERROR: Company description generation failed for {ticker}: {e}")
        raise


def generate_platform_diagram(
    ticker: str,
    profile_description: str,
    sector: str | None,
    industry: str | None,
    form_10k_text: str,
    transcript_excerpts: str,
    segment_names: list[str],
    fiscal_year: int | None,
) -> str:
    """Synthesize a monospace platform-overview diagram for §2.

    Reads the same 10-K + profile inputs used by `generate_company_description`
    plus excerpts from the two most recent earnings transcripts (Q&A segments)
    so the visual reflects how management currently frames the platform —
    investor decks aren't on disk, but call language is the closest proxy for
    the "platform diagram slide" they'd use.

    Returns a JSON string the caller parses. Schema:
      {
        "diagram": "<fenced monospace block, <= 80 cols, box-drawing chars>",
        "caption": "1-2 sentence caption explaining the diagram"
      }
    """
    segments_block = (
        "\n".join(f"- {n}" for n in segment_names) if segment_names else "(none on file)"
    )
    prompt = f"""You are an equity analyst building a "platform overview" visual for a research brief on {ticker}.

The goal is a compact ASCII / monospace diagram that captures HOW the
company's platform connects inputs (customers, suppliers, capital) to
outputs (products, revenue). Think of the kind of "platform slide" an
investor deck would show — the one diagram that, on its own, communicates
what the business is.

INPUTS

Sector: {sector or "(unknown)"}
Industry: {industry or "(unknown)"}
Fiscal year of source 10-K: {fiscal_year or "(unknown)"}

FMP profile.json description:
\"\"\"
{profile_description.strip() or "(none)"}
\"\"\"

10-K narrative excerpts (business description / segments):
\"\"\"
{form_10k_text.strip() or "(no 10-K narrative available)"}
\"\"\"

Recent earnings-call Q&A excerpts (how management frames the platform NOW):
\"\"\"
{transcript_excerpts.strip() or "(no transcripts available)"}
\"\"\"

Segment names this report displays (use these where they fit; do not invent new ones):
{segments_block}

---

Produce a JSON object with EXACTLY these keys (no markdown around the JSON, no commentary):

{{
  "diagram": "<a single monospace block, 60-78 columns wide, drawn with Unicode box-drawing chars (┌─┐│└┘├┤┬┴┼) and directional arrows (→ ← ↑ ↓). NO leading or trailing fence markers — the renderer wraps the block in a code fence itself.>",
  "caption": "1-2 sentences (plain prose, no markdown) explaining what the diagram shows and what makes the platform distinctive"
}}

Diagram rules:
- Aim for THREE conceptual columns or layers, e.g. (Inputs/Customers) → (Platform/Core) → (Products/Revenue).
  Use what fits this business; a two-sided marketplace is two columns flowing into a middle, a vertical SaaS is a stack, etc.
- Each box should hold a short label (2-5 words) plus optionally a one-line concrete detail (a KPI, a count, a product name).
- Width: keep every line <= 78 characters. Height: aim for 8-16 lines total. The block must look balanced when rendered in a fixed-width font.
- Use ONLY: ┌ ─ ┐ │ └ ┘ ├ ┤ ┬ ┴ ┼ → ← ↑ ↓ plus ASCII letters, digits, spaces, common punctuation. No emoji, no Markdown bold, no HTML.
- Anchor the labels in concrete facts from the inputs above. If a fact isn't in the inputs, leave the label generic rather than invent.
- Do NOT escape characters inside the JSON string except for required \\n (newlines between diagram rows) and \\". The renderer interprets the string as-is in a code fence.

Caption rules:
- Plain English. Reference the platform's distinctive mechanism (network effect, vertical integration, data flywheel, regulatory moat, etc.) only if the inputs actually support it.
- Do NOT restate the diagram literally; add the "why this shape" insight.

Return strictly the JSON object — no prose before or after, no markdown fence around the JSON.
"""
    try:
        raw = call_llm(prompt, purpose="platform_diagram", ticker=ticker).strip()
        if raw.startswith("```"):
            raw = JSON_FENCE_RE.sub("", raw).strip()
        return raw
    except Exception as e:
        log.error(f"CRITICAL ERROR: Platform diagram generation failed for {ticker}: {e}")
        raise


def classify_intake_document(filename: str, text: str, hint: dict) -> dict | None:
    """
    Classify a user-dropped IR document.

    Returns a dict with keys (ticker, period_end, doc_type, confidence, reasoning)
    matching `src.intake.IntakeClassification`, or None on any LLM/parse failure.
    Routes through the fast classifier model — this is a short structured call
    that runs ~50x per intake batch, so latency matters more than raw quality.
    """
    prompt = (
        "You are classifying an investor-relations document for a portfolio analyst.\n\n"
        "Given the filename and an excerpt of the document text, return a JSON object with EXACTLY these fields:\n\n"
        "{\n"
        '  "ticker": "<US-listed primary ticker, e.g. NVDA, GOOG, BN, MELI>",\n'
        '  "period_end": "<YYYY-MM-DD, the last calendar day of the fiscal quarter>",\n'
        '  "doc_type": "<one of: ir_press_release, ir_presentation, ir_supplement, ir_investor_update, earnings_call_transcript, ir_event>",\n'
        '  "confidence": <float 0.0 to 1.0>,\n'
        '  "reasoning": "<one sentence explaining your choice>"\n'
        "}\n\n"
        "Doc-type guidance (pick the dominant form, not the topic):\n"
        "- ir_press_release: short text-heavy quarterly earnings announcement / financial results\n"
        "- ir_presentation: slide deck dominated by charts / visuals / bullet slides for a quarter\n"
        "- ir_supplement: detailed financial supplement / data book / spreadsheet-style tables\n"
        "- ir_investor_update: longer letter to shareholders / quarterly update narrative\n"
        "- earnings_call_transcript: speaker-attributed dialogue from the earnings call\n"
        "- ir_event: NON-QUARTERLY IR materials — investor day, AGM, capital markets day,\n"
        "    conference deck, ad-hoc strategic announcement, M&A or stock-split deck.\n"
        "    These are NOT tied to a fiscal quarter. For these, period_end = the EVENT DATE\n"
        "    (the day the event occurred), not a quarter-end. If you can't find an exact day\n"
        "    in the document, use the first day of the relevant month or the cover-page year.\n\n"
        "Period-end mapping (for quarterly doc types only):\n"
        "- Calendar fiscal year (BN, MELI, GOOG, META, NVO, NU, NOW, WIX, AMZN): Q1=03-31, Q2=06-30, Q3=09-30, Q4=12-31.\n"
        "- VEEV / RBRK have January fiscal year-end. FY26 Q1 ends ~04-30, Q2 ~07-31, Q3 ~10-31, Q4 ~01-31 of the next calendar year.\n"
        "- NVO publishes H1 (map to Q2, period_end 06-30) and 9M (map to Q3, 09-30).\n\n"
        "Set confidence < 0.6 if the document is empty, ambiguous, or clearly not an IR document for a tracked holding.\n\n"
        f"Filename hint (pre-extracted, may be wrong): {hint}\n"
        f"Filename: {filename}\n\n"
        "Document text excerpt:\n"
        '"""\n'
        f"{text[:INTAKE_TEXT_BUDGET]}\n"
        '"""\n\n'
        "Return ONLY the JSON object — no prose, no markdown fence."
    )

    try:
        raw = call_llm(prompt, purpose="intake_classifier").strip()
        if raw.startswith("```"):
            raw = JSON_FENCE_RE.sub("", raw).strip()
        return json.loads(raw)
    except Exception as e:
        log.error(f"classify_intake_document failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Valuation basis (Opus picks ONE multiple per ticker)
# ---------------------------------------------------------------------------

# Allowed multiples — the compute layer knows how to fetch the numeric value
# for each. The LLM's choice MUST be in this set; out-of-set picks get
# rejected by the parser and fall back to a sector-default heuristic.
VALUATION_MULTIPLE_CHOICES: tuple[str, ...] = (
    "EV/NTM Revenue",
    "EV/LTM Revenue",
    "EV/NTM EBITDA",
    "EV/LTM EBITDA",
    "P/E (NTM)",
    "P/E (LTM)",
    "P/B",
    "P/TBV",
    "P/FCF",
    "EV/FCF",
)


def generate_valuation_basis(
    ticker: str,
    sector: str | None,
    industry: str | None,
    thesis_text: str,
    financial_profile_md: str,
    available_estimates_md: str,
) -> str:
    """Pick the ONE valuation multiple that best frames this business.

    Returns a JSON string the caller parses. Schema:
      {
        "multiple": "<one of VALUATION_MULTIPLE_CHOICES>",
        "rationale": "<1-2 sentence why this is the right lens for THIS ticker>",
        "target_band": "<optional qualitative target read, e.g. 'historical range 12-18x; deserves the upper half given GCP margin trajectory'>",
        "notes": "<optional caveat, e.g. 'NTM not available — fell back to LTM'>"
      }

    The LLM picks based on:
    - Sector / industry conventions (banks: P/B or P/TBV; SaaS: EV/NTM Revenue;
      capital-intensive: EV/EBITDA; cyclicals: through-cycle P/E; etc.)
    - The actual investment thesis (if thesis hinges on FCF inflection,
      P/FCF lens; if on top-line, EV/Revenue; etc.)
    - What's computable — only pick NTM-based multiples when analyst
      estimates are listed as available.
    """
    choices_block = "\n".join(f"- {m}" for m in VALUATION_MULTIPLE_CHOICES)
    prompt = f"""You are a senior buy-side analyst picking THE SINGLE multiple
that best frames {ticker} for an investment memo. Not 2-3 multiples. ONE.
The reader will see this number prominently on the report's Valuation tab;
your pick must answer the question "is this stock rich or cheap?" in the
lens that's most diagnostic for THIS specific business.

TICKER: {ticker}
SECTOR / INDUSTRY: {sector or "?"} / {industry or "?"}

THESIS (the analyst's investment case):
\"\"\"
{thesis_text.strip()[:2000] or "(no thesis on file)"}
\"\"\"

FINANCIAL PROFILE (recent quarterly + TTM shape):
\"\"\"
{financial_profile_md.strip()[:2500]}
\"\"\"

AVAILABLE ANALYST ESTIMATES (use to know which NTM multiples are computable):
\"\"\"
{available_estimates_md.strip()[:1200]}
\"\"\"

PICK FROM EXACTLY THESE OPTIONS (verbatim string match):
{choices_block}

Selection guidance:
- Banks / fintech-with-balance-sheet (NU, MELI's credit book, SOFI): P/B or P/TBV is the canonical lens. P/E only if earnings power is the bet.
- SaaS / high-growth software with negative or thin GAAP earnings: EV/NTM Revenue, EV/LTM Revenue as fallback.
- Profitable platforms / GARP (GOOG, META, NOW at scale): EV/NTM EBITDA or P/E (NTM) when consensus EBITDA / EPS is available.
- Capital-intensive / industrial / commodity (BHP, FCX, CGEH): EV/LTM EBITDA — through-cycle.
- FCF-thesis names (mature compounders, royalty/lease businesses): P/FCF or EV/FCF.
- If the thesis is explicitly about FCF inflection or capex moderation, prefer the FCF multiples regardless of sector default.
- Only pick NTM multiples when the AVAILABLE ANALYST ESTIMATES block lists the relevant NTM line.
- Note: choosing P/E (NTM) additionally surfaces a PEG ratio (forward P/E divided by forward EPS growth) on the Valuation tab — the diagnostic lens when the thesis is about earnings compounding and "cheap or rich vs growth" is the question. PEG is auto-omitted for every other multiple and for unprofitable / negative-growth names, so weigh it as a point in P/E (NTM)'s favor only when forward EPS growth is genuinely the bet.

{NUMBER_FORMATTING_BLOCK}

Return ONLY a JSON object (no markdown fence, no prose):

{{
  "multiple": "<one of the options above, exact string>",
  "rationale": "1-2 sentence why THIS multiple is the diagnostic lens for THIS ticker's thesis. Reference the specific thesis pillar or business-model mechanic that makes it the right pick. Generic 'standard SaaS lens' earns a rewrite.",
  "target_band": "Optional 1-sentence qualitative read of where the multiple SHOULD trade (e.g. 'historical 10-15x range; deserves the upper half if margin expansion sustains', or 'currently in a re-rating window — base-case 4-6x P/TBV'). Pass empty string if no view.",
  "notes": "Optional 1-line caveat, e.g. 'NTM not available, fell back to LTM' or 'historical P/B distorted by 2022 IPO multiple compression'. Empty string if none."
}}
"""
    try:
        raw = call_llm(prompt, purpose="valuation_basis", ticker=ticker).strip()
        if raw.startswith("```"):
            raw = JSON_FENCE_RE.sub("", raw).strip()
        return raw
    except Exception as e:
        log.error(f"CRITICAL ERROR: Valuation basis generation failed for {ticker}: {e}")
        raise


# ---------------------------------------------------------------------------
# SayDo importance ranking (Opus orders pairwise SayDo bullets by thesis impact)
# ---------------------------------------------------------------------------


def generate_saydo_importance(
    ticker: str,
    quarter_label: str,
    bullets: list[dict[str, str]],
    anchor_block: str = "",
) -> str:
    """Order a list of SayDo bullet snippets by thesis impact.

    ``bullets`` is ``[{"id": str, "snippet": str}, ...]`` where each snippet
    is one short paragraph or bullet from the pairwise SayDo analysis (e.g.
    "Revenue: guided $X, printed $Y, +5% delta..." or "CLIP rollout pace...").

    Returns a JSON list of the IDs in importance order (most thesis-relevant
    first). Bullets the LLM judges immaterial may be omitted. Up to 8 kept.

    Used by the renderer to sort the pairwise SayDo card before display so
    the top of each card is what actually matters for the thesis, not what
    happened to appear first in the LLM's pairwise output.
    """
    payload = json.dumps(bullets, ensure_ascii=False)
    prompt = f"""You are ranking SayDo (commitments vs. delivery) bullets from
the {ticker} {quarter_label} pairwise analysis BY THESIS IMPACT — what
moves the investment case the most, not what was disclosed first.

{anchor_block}BULLETS (JSON):
{payload}

RANKING rules:
- A bullet that moves a TIER-1 KPI ranks above one that doesn't.
- A bullet that confirms or refutes a named bear-case failure mode ranks
  high regardless of magnitude (it resolves analytical uncertainty).
- Items that are bookkeeping / FX / share-count / tax-rate / one-off
  accounting noise rank LOW and should be dropped if you have more than 8
  bullets.
- Items framed as "what didn't happen" or "what's unchanged" can still be
  important if the unchanged direction confirms a thesis pillar.

Return ONLY a JSON array of the IDs in importance order (up to 8):

["2", "0", "5", "1", ...]

Echo IDs verbatim from input. Do NOT invent IDs. Do NOT include omitted
bullets.
"""
    try:
        raw = call_llm(prompt, purpose="saydo_importance", ticker=ticker).strip()
        if raw.startswith("```"):
            raw = JSON_FENCE_RE.sub("", raw).strip()
        return raw
    except Exception as e:
        log.error(f"CRITICAL ERROR: SayDo importance ranking failed for {ticker}: {e}")
        raise


# ---------------------------------------------------------------------------
# Earnings themes split — prepared remarks vs Q&A theme rollup (Phase 2c §5)
# ---------------------------------------------------------------------------

# Per-transcript section character cap. Each side of each quarter is trimmed
# to this length before prompt assembly so 4 quarters × 2 sides stays within
# Sonnet's effective context window. Sized to preserve the bulk of a typical
# Q&A segment (~30k chars uncompressed) while keeping the prompt under ~200k.
_THEMES_SECTION_CHAR_CAP = 22000


def extract_qa_vs_prepared_themes(
    ticker: str,
    transcripts: list[dict[str, object]],
    ts_signals_md: str = "",
) -> str:
    """Roll up recurring themes across 4 quarters, split prepared vs Q&A.

    ``transcripts`` is a chronological list (oldest → newest) of
    ``{"period": "Q1 2026", "prepared": str | None, "qa": str | None}``
    dicts. ``prepared`` / ``qa`` are the raw text segments for that quarter;
    either may be None when the source transcript didn't carry that
    section (e.g. Q&A-only aggregator pulls have prepared=None).

    ``ts_signals_md`` is a markdown block of pre-computed time-series
    signals (revenue / OI / EPS / FCF / margins + tier-1 KPI trends,
    inflections, anomalies). Surfaced to both rollups so that what the
    LLM considers "noteworthy" in prepared vs Q&A is interpretable
    against the trailing pattern (e.g. a Q&A question about margin
    compression matches a yellow-severity OI-margin anomaly signal).
    Empty string is fine — caller-side gate, prompt shape unchanged.

    Returns the raw JSON string the caller parses. Schema:

      {
        "prepared_themes": [
          {
            "theme_name": "AI compute capacity allocation",
            "mentions_per_quarter": {"Q1 2026": 3, "Q4 2025": 2, ...},
            "evidence": [
              {"period": "Q1 2026", "speaker": "Sundar Pichai",
               "text": "we are constrained by compute..."}
            ]
          }, ...
        ],
        "qa_themes": [ ... same shape ... ]
      }

    The caller is responsible for stripping ``prepared_themes`` /
    ``qa_themes`` to [] when the corresponding input side was empty across
    every quarter (the LLM is instructed to do this but we defensively
    enforce in the section builder).
    """
    # Build per-section blocks separately so the LLM sees the prepared/Q&A
    # boundary explicitly. Period labels appear in every block so cross-
    # quarter mention counting is grounded in the same labels we'll render.
    prepared_blocks: list[str] = []
    qa_blocks: list[str] = []
    any_prepared = False
    any_qa = False
    for t in transcripts:
        period = str(t.get("period") or "")
        prepared = t.get("prepared")
        qa = t.get("qa")
        if isinstance(prepared, str) and prepared.strip():
            any_prepared = True
            prepared_blocks.append(
                f"### {period} — prepared remarks\n{prepared[:_THEMES_SECTION_CHAR_CAP]}\n"
            )
        if isinstance(qa, str) and qa.strip():
            any_qa = True
            qa_blocks.append(
                f"### {period} — analyst Q&A\n{qa[:_THEMES_SECTION_CHAR_CAP]}\n"
            )

    prepared_block = "\n".join(prepared_blocks) if prepared_blocks else "(no prepared-remarks segments available across the window)"
    qa_block = "\n".join(qa_blocks) if qa_blocks else "(no Q&A segments available across the window)"
    periods = [str(t.get("period") or "") for t in transcripts]
    periods_csv = ", ".join(periods)

    prompt = f"""You are surfacing the analyst-side narrative shape of {ticker}'s
last {len(transcripts)} earnings calls. Two distinct cross-quarter rollups:

1. PREPARED-REMARKS THEMES — what MANAGEMENT chose to lead with across calls.
   These show the company's own narrative — capital allocation framing,
   product positioning, recurring strategic talking points.
2. Q&A THEMES — what ANALYSTS pressed management on across calls. These show
   buy-side concern shape — what the street keeps asking about,
   contentious topics, areas of management dodging.

The contrast between the two is itself the signal: a topic that dominates
prepared remarks but never gets pressed on means management is leading the
narrative successfully; a topic that recurs in Q&A but is absent from
prepared remarks means the street has a concern management isn't addressing.

PERIODS in chronological order (oldest → newest): {periods_csv}
{_ts_signals_prompt_block(ts_signals_md)}
=== PREPARED REMARKS BLOCKS ===
{prepared_block}

=== Q&A BLOCKS ===
{qa_block}

---

Produce a JSON object with EXACTLY these keys (no markdown, no commentary):

{{
  "prepared_themes": [
    {{
      "theme_name": "concise 4-7 word noun phrase (e.g. 'AI compute capacity allocation', 'CapEx trajectory through 2027')",
      "mentions_per_quarter": {{"Q1 2026": 3, "Q4 2025": 2, ...}},
      "evidence": [
        {{"period": "Q1 2026", "speaker": "Sundar Pichai", "text": "verbatim short quote (1-2 sentences max)"}}
      ]
    }}
  ],
  "qa_themes": [
    {{
      "theme_name": "concise 4-7 word noun phrase",
      "mentions_per_quarter": {{...}},
      "evidence": [
        {{"period": "Q1 2026", "speaker": "Brian Nowak (Morgan Stanley)", "text": "verbatim short analyst question or follow-up"}}
      ]
    }}
  ]
}}

HARD RULES:
- 3-5 themes per side. If a side has no source material at all, emit `[]`
  for that side and DO NOT invent themes from the other side's content.
- `theme_name` must be a NOUN PHRASE, not a sentence. "Capital allocation
  framing" is valid; "Management discussed capital allocation" is not.
- `mentions_per_quarter` keys MUST be drawn from the PERIODS list above
  verbatim. Omit any quarter where the theme didn't surface — do NOT
  pad with zero counts.
- Counts are conservative — count distinct discussion turns, not every
  word reference. A single Q&A exchange that touches the theme counts as 1.
- `evidence` carries 1-2 verbatim quotes per theme. Each quote must be
  pulled from the appropriate source side (prepared themes draw from
  prepared blocks, Q&A themes draw from Q&A blocks). Strip filler /
  cross-talk; quote 1-2 sentences max each. Use speaker attribution
  exactly as it appears in the transcript when available; otherwise pass
  an empty string for speaker.
- Order themes by total mention count (`sum(mentions_per_quarter.values())`)
  descending — most recurring themes first.
- Themes within a side must be DISTINCT. If two candidate themes overlap,
  pick the more specific framing and drop the broader one.

Return strictly the JSON object — nothing else.
"""

    if not any_prepared and not any_qa:
        # No source material at all — surface that to the caller so it can
        # short-circuit without an LLM call. Return the canonical empty
        # shape; the caller's parse path will produce empty rollups.
        return '{"prepared_themes": [], "qa_themes": []}'

    try:
        raw = call_llm(prompt, purpose="earnings_themes_split", ticker=ticker).strip()
        if raw.startswith("```"):
            raw = JSON_FENCE_RE.sub("", raw).strip()
        return raw
    except Exception as e:
        log.error(f"CRITICAL ERROR: Earnings themes split failed for {ticker}: {e}")
        raise
