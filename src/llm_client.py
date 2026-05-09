"""
src/llm_client.py
-----------------
LLM client for the earnings-summary pipeline. Two-tier execution:

  PRIMARY: Claude Code CLI (`claude -p`) via subprocess. Bills against the user's
  Claude Pro/Max subscription. Zero per-call cost.

  FALLBACK: Gemini via google-generativeai. Fires automatically when the Claude
  CLI call fails (timeout, non-zero exit, empty output, binary missing).
  Per-call cost on Gemini Flash is sub-penny; the fallback prevents single-ticker
  failures from blocking batch runs.

CRITICAL: The Claude Agent SDK does NOT support subscription billing — it
requires ANTHROPIC_API_KEY (separate API billing). The CLI is the ONLY path
to subscription billing. Even the CLI silently falls back to API billing if
ANTHROPIC_API_KEY is set in the environment, so the module's lazy setup check
fails loud when that key is present. The Gemini fallback is intentionally a
separate, opt-in path that requires its own GEMINI_API_KEY in .env — it cannot
be triggered by accidentally setting ANTHROPIC_API_KEY.

Setup (one-time, user action required):
1. Install Claude Code CLI: see https://code.claude.com/docs/en/setup
2. Authenticate to your subscription: `claude auth login`
3. Ensure ANTHROPIC_API_KEY is unset in the shell that runs this pipeline:
     PowerShell: Remove-Item env:ANTHROPIC_API_KEY
     Bash:       unset ANTHROPIC_API_KEY
4. (Optional but recommended) Add GEMINI_API_KEY to .env to enable the fallback
   path. Without it, Claude CLI failures surface as hard errors. See .env.example.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from datetime import date

# NOTE: `google.generativeai` is deprecated as of late 2025; Google's path forward
# is `google-genai` with a different API (genai.Client / client.models.generate_content).
# Migrate when convenient — the deprecated package still works and avoids forcing
# users to re-install for the fallback path. See:
# https://github.com/google-gemini/deprecated-generative-ai-python
import warnings
with warnings.catch_warnings():
    warnings.simplefilter("ignore", FutureWarning)  # silence deprecation noise at import
    import google.generativeai as genai

from dotenv import load_dotenv

# Load .env at module init so GEMINI_API_KEY is available without callers having to
# import dotenv themselves. Silent no-op if .env doesn't exist.
load_dotenv()

# Staleness threshold (days). When the most-recent evidence in the corpus is
# older than this vs. the report date, the tracker switches to STALE-CORPUS
# mode (different scorecard columns + conviction cap).
STALE_CORPUS_THRESHOLD_DAYS = 120

# Default Claude model for prompt calls. Sonnet 4.6 chosen as a balance of
# quality and speed across the pipeline's tasks. Per-function overrides via
# the `model` argument on _call_claude.
DEFAULT_MODEL = "claude-sonnet-4-6"

# Fast classifier model — used for short, structured calls (intake doc-type
# classification, transcript metadata extraction) where Sonnet would be
# overkill. Haiku 4.5 returns ~5x faster at materially the same quality on
# narrowly-scoped JSON-output tasks.
FAST_CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"

# Default per-call timeout (seconds). Long-context thesis prompts can take
# a few minutes on Sonnet; the cap protects against runaway hangs. 20 min
# leaves headroom for the heaviest cases (4-quarter ticker × dense schema)
# while still catching CLI hangs in a reasonable wall time.
DEFAULT_TIMEOUT_SECONDS = 1200

# Schema fields stripped from the LLM prompt — they are audit-trail metadata
# meant for the human reviewer (why a schema was edited, when it was last
# revised) and bloat the prompt without aiding the analysis. Centralized so
# both passes apply the same redaction.
SCHEMA_LLM_REDACT_FIELDS: frozenset[str] = frozenset({
    "thesis_status_note",
    "schema_revision_notes",
    "last_updated",
})

# Markdown JSON-fence stripper — `claude -p` occasionally wraps structured
# JSON responses in ```json ... ``` fences even when asked not to. Used by
# functions that demand strict JSON output (classify_intake_document, etc.).
JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

# Intake classifier prompt budget — keep document excerpts bounded so a
# single oversized PDF doesn't blow the prompt. 6 KB matches main's prior
# Gemini-Flash budget; well under any Claude model's context.
INTAKE_TEXT_BUDGET = 6000

# Gemini fallback model. Single Flash variant for both heavy (thesis tracker)
# and light (intake classifier) workloads — quality is good enough for backup
# duty and per-call cost is sub-penny. Override per-call by passing a custom
# model to _try_gemini_fallback if needed.
GEMINI_FALLBACK_MODEL = "gemini-2.5-flash"

log = logging.getLogger(__name__)

_setup_verified: bool = False
_claude_cli_path: str | None = None


def _verify_setup_once() -> None:
    """
    Lazy environment check on first LLM call — fails loud rather than mis-billing.
    Also resolves and caches the absolute path to the `claude` binary, because
    on Windows the CLI is installed as `claude.cmd` and Python's subprocess does
    not apply PATHEXT to bare names — passing "claude" would CreateProcess-fail.
    """
    global _setup_verified, _claude_cli_path
    if _setup_verified:
        return
    if "ANTHROPIC_API_KEY" in os.environ:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is set in the environment. The Claude Code CLI will silently "
            "route calls to API billing instead of your subscription. Unset it before running:\n"
            "  PowerShell:  Remove-Item env:ANTHROPIC_API_KEY\n"
            "  Bash:        unset ANTHROPIC_API_KEY"
        )
    resolved = shutil.which("claude")
    if resolved is None:
        raise RuntimeError(
            "Claude Code CLI ('claude') not found in PATH. Install it from "
            "https://code.claude.com/docs/en/setup, then run `claude auth login`."
        )
    _claude_cli_path = resolved
    _setup_verified = True


def _try_gemini_fallback(prompt: str, claude_error: Exception) -> str:
    """
    Last-resort Gemini call invoked when the Claude CLI fails operationally
    (timeout, non-zero exit, empty stdout, binary missing). Reads GEMINI_API_KEY
    (or GOOGLE_API_KEY) from the environment — populated by load_dotenv() at
    module init.

    If no Gemini key is available, re-raises a RuntimeError that wraps the
    original Claude error and points the user at .env.example. This makes the
    failure mode explicit: setup an LLM, see the prompt, fix the cause.

    Setup-class Claude errors (ANTHROPIC_API_KEY set, claude binary missing)
    are NOT routed here — they propagate from _verify_setup_once() before the
    subprocess call ever runs.
    """
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Claude CLI failed AND no Gemini fallback configured. "
            f"Original Claude error: {type(claude_error).__name__}: {str(claude_error)[:300]}\n"
            "Add GEMINI_API_KEY=<your-key> to .env to enable the fallback path. "
            "See .env.example."
        ) from claude_error

    log.warning({
        "event": "claude_cli_failed_falling_back_to_gemini",
        "claude_error": f"{type(claude_error).__name__}: {str(claude_error)[:200]}",
        "gemini_model": GEMINI_FALLBACK_MODEL,
    })
    genai.configure(api_key=api_key)
    model_obj = genai.GenerativeModel(GEMINI_FALLBACK_MODEL)
    response = model_obj.generate_content(prompt)
    text = (response.text or "").strip() if hasattr(response, "text") else ""
    if not text:
        raise RuntimeError(
            "Both LLMs failed: Claude CLI errored AND Gemini fallback returned empty response.\n"
            f"Claude error: {type(claude_error).__name__}: {str(claude_error)[:200]}"
        ) from claude_error
    log.info({"event": "gemini_fallback_done", "response_chars": len(text)})
    return text


def _call_claude(
    prompt: str,
    model: str = DEFAULT_MODEL,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """
    Single-shot LLM call. Tries the Claude Code CLI first (subscription billing).
    On any operational failure — timeout, non-zero exit, empty output, or the
    binary becoming unavailable mid-run — falls back to Gemini Flash if a
    GEMINI_API_KEY is available in the environment.

    Setup errors (ANTHROPIC_API_KEY set, claude binary missing on first call)
    raise RuntimeError directly without invoking the fallback — those need to
    be fixed by the operator, not papered over.

    Prompts are passed via stdin to avoid Windows CreateProcess command-line
    length limits (32K).
    """
    _verify_setup_once()  # setup errors propagate; do NOT route to fallback
    assert _claude_cli_path is not None  # set by _verify_setup_once when it returns successfully
    log.info({"event": "llm_call_start", "model": model, "prompt_chars": len(prompt)})
    try:
        result = subprocess.run(
            [_claude_cli_path, "-p", "--model", model],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",  # Force UTF-8 — Windows otherwise defaults to cp1252 which dies on
            errors="replace",  # common financial-doc Unicode (U+2212 minus, en/em dashes, arrows).
            check=True,
            timeout=timeout_seconds,
        )
        text = result.stdout.strip()
        if not text:
            raise RuntimeError(f"claude -p returned empty stdout. stderr: {result.stderr.strip()[:200]}")
        log.info({"event": "llm_call_done", "response_chars": len(text)})
        return text
    except (subprocess.SubprocessError, OSError, RuntimeError) as claude_error:
        # Operational failure — try Gemini fallback. _try_gemini_fallback raises
        # if no Gemini key is configured, surfacing both errors together.
        return _try_gemini_fallback(prompt, claude_error)


# Web-search-enabled call: same subprocess as _call_claude but with the
# Claude CLI's --allowedTools flag turned on so the model can run WebSearch
# / WebFetch as part of producing its answer. Used by the memo generator
# for the "Recent Developments" section so memos cite real news URLs
# instead of leaning on a stale FMP news pre-pull.
CLAUDE_WEB_TOOLS = "WebSearch WebFetch"
CLAUDE_WEB_TIMEOUT_SECONDS = 1800  # web fetches add round-trips; bigger cap


def call_llm_with_web(
    prompt: str,
    model: str = DEFAULT_MODEL,
    timeout_seconds: int = CLAUDE_WEB_TIMEOUT_SECONDS,
) -> str:
    """LLM call with Claude WebSearch + WebFetch tools enabled.

    Setup invariants are the same as `_call_claude` (subscription billing
    via the CLI, UTF-8, stdin prompt). On Claude failure, falls through to
    plain `_call_claude` (which has its own Gemini fallback) so a memo is
    always produced even when web tools are unavailable.

    Use for memo generation, fact-finding on recent news, anything where
    the upstream context is stale and Claude needs to look something up.
    """
    _verify_setup_once()
    assert _claude_cli_path is not None
    log.info({"event": "llm_web_call_start", "model": model, "prompt_chars": len(prompt)})
    cmd = [
        _claude_cli_path, "-p", "--model", model,
        "--allowedTools", *CLAUDE_WEB_TOOLS.split(),
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
        )
        text = result.stdout.strip()
        if not text:
            raise RuntimeError(
                f"claude -p with web tools returned empty stdout. stderr: {result.stderr.strip()[:200]}"
            )
        log.info({"event": "llm_web_call_done", "response_chars": len(text)})
        return text
    except (subprocess.SubprocessError, OSError, RuntimeError) as web_err:
        log.warning({
            "event": "llm_web_call_fallback_to_plain",
            "error": f"{type(web_err).__name__}: {web_err}",
        })
        # Fall through to non-web path so the caller still gets output.
        return _call_claude(prompt, model=model, timeout_seconds=timeout_seconds)


# ---------------------------------------------------------------------------
# Prompt-bearing functions (signatures preserved — callers unchanged)
# ---------------------------------------------------------------------------


def generate_pairwise_analysis(prev_summary, curr_summary):
    """
    Generates a specific "Say-Do" analysis comparing two sequential quarters.
    """
    prev_q_str = f"{prev_summary['quarter']} {prev_summary['year']}"
    curr_q_str = f"{curr_summary['quarter']} {curr_summary['year']}"

    prompt = f"""
    You are a Strategic Management Consultant and Senior Equity Analyst.
    **Task:** Perform a strict "Say-Do" analysis comparing the **Outlook/Guidance** from the Previous Quarter ({prev_q_str}) against the **Actual Results** reported in the Current Quarter ({curr_q_str}).

    **Hard rules — non-negotiable:**
    - Every numeric figure, dated event, and management quote must be traceable to the input summaries below. If something is not present, write `[not disclosed]`. Do not invent, infer, or back-fill from prior knowledge of this company.
    - Verbatim quotes belong in quotation marks with a source tag like `[Source: {prev_q_str} prepared remarks]` or `[Source: {curr_q_str} Q&A]`.
    - The Attribution call (Execution vs. Exogenous) is a judgment, so it MUST go through the adversarial loop below — no shortcut to a verdict.

    **Input Data:**
    1.  **Previous Quarter ({prev_q_str}) Summary:**
        {prev_summary['text']}

    2.  **Current Quarter ({curr_q_str}) Summary:**
        {curr_summary['text']}

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
    *   [Structural vs. temporary blip — must follow from §4, not asserted independently.]
    """

    try:
        return _call_claude(prompt)
    except Exception as e:
        log.error(f"Error generating pairwise analysis: {e}")
        return f"Could not generate analysis for {prev_q_str} -> {curr_q_str}."


def generate_summary(text):
    """
    Generates a 1-2 page summary of the earnings transcript.
    """
    prompt = """
    You are an expert financial analyst. Please provide a detailed 1-2 page summary of the provided earnings call transcript.

    **STRICT CONSTRAINT:** Do not provide conversational filler. Start your response immediately with the Report Title.

    **Output Format (Strict Markdown):**

    # Earnings Call Summary: [Company Ticker] [Quarter] [Year]

    ## 1. Executive Summary
    *   **High-Level Narrative:** [2-3 sentences on the main story of the quarter]
    *   **Segment Performance:** [Brief overview by business unit]

    ## 2. Financial Highlights
    | Metric | Value | QoQ | YoY | Miss/Beat |
    | :--- | :--- | :--- | :--- | :--- |
    | **Revenue** | [Value] | [Growth] | [Growth] | [Context] |
    | **EPS** | [Value] | [Growth] | [Growth] | [Context] |
    | **Gross Margin**| [Value] | [Change] | [Change] | [Context] |
    | **Op. Inc.** | [Value] | [Change] | [Change] | [Context] |

    *   **Key Drivers:** [Text analysis of what drove the numbers]

    ## 3. Operational Highlights
    *   **Product:** [Launches, updates]
    *   **Regional:** [Geo performance]
    *   **Strategic Initiatives:** [M&A, Partnerships, Restructuring]

    ## 4. Management Outlook (Guidance)
    *   **Next Quarter:** [Revenue, EPS, Margin targets]
    *   **Full Year:** [Updated FY guidance]
    *   **Commentary:** [CEO/CFO sentiment, headwinds/tailwinds]

    ## 5. Q&A Key Points
    *   **Analyst Concerns:** [Top 2-3 contentious questions]
    *   **Management Response:** [How they answered usually defense or explanation]

    Transcript:
    """
    try:
        return _call_claude(prompt + text)
    except Exception as e:
        log.error(f"CRITICAL ERROR: Summary generation failed: {e}")
        raise


def generate_press_release_summary(text: str) -> str:
    """
    Generates a structured summary from an earnings press release.
    Press releases are financial-forward — emphasize the numbers table and guidance.
    """
    prompt = """
You are an expert financial analyst. Summarize the following earnings press release.
This is sourced from the company's IR website, so it is the official financial release — be precise.

**STRICT CONSTRAINT:** Do not provide conversational filler. Start your response immediately with the Report Title.

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
        return _call_claude(prompt + text)
    except Exception as e:
        log.error(f"CRITICAL ERROR: Press release summary generation failed: {e}")
        raise


def generate_presentation_brief(text: str) -> str:
    """
    Generates a strategic brief from an earnings presentation slide deck.
    Presentations are typically 20–40 pages of slides; extract the key strategic narrative.
    """
    prompt = """
You are a senior equity research analyst. The following text was extracted from an earnings presentation slide deck.
Extract the key strategic narrative — what story is management telling investors?

**STRICT CONSTRAINT:** Do not provide conversational filler. Start your response immediately with the Report Title.

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
        return _call_claude(prompt + text)
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


def _compute_staleness(report_date: str, corpus_latest_date: str | None) -> tuple[int, bool, str, str]:
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
    scorecard_target_col = "vs. Latest Disclosed Forward Target" if is_stale else "vs. Break Threshold"
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
        lines.extend([
            "> **CORPUS STALENESS DISCLAIMER**: latest evidence in this tracker is "
            f"{corpus_latest_date} — {staleness_days} days stale vs. report date {report_date} "
            f"(threshold: {STALE_CORPUS_THRESHOLD_DAYS} days). Verdicts apply STALE-CORPUS MODE: "
            "scorecard compares vs. latest disclosed forward target rather than break thresholds, "
            "and adversarial-loop conviction is capped at Low absent explicit in-corpus justification.",
            "",
        ])

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
        ticker, schema, quarters, report_date,
        staleness_line, is_stale, staleness_directive, quarters_context,
    )
    log.info({"event": "thesis_pass_start", "ticker": ticker, "pass": "A"})
    try:
        pass_a_output = _call_claude(pass_a_prompt)
    except Exception as e:
        log.error(f"CRITICAL ERROR: Thesis Pass A failed for {ticker}: {e}")
        raise
    log.info({"event": "thesis_pass_done", "ticker": ticker, "pass": "A", "output_chars": len(pass_a_output)})

    pass_b_prompt = _build_pass_b_prompt(
        ticker, schema, quarters, report_date,
        staleness_line, staleness_directive, quarters_context, pass_a_output,
    )
    log.info({"event": "thesis_pass_start", "ticker": ticker, "pass": "B"})
    try:
        pass_b_output = _call_claude(pass_b_prompt)
    except Exception as e:
        log.error(f"CRITICAL ERROR: Thesis Pass B failed for {ticker}: {e}")
        raise
    log.info({"event": "thesis_pass_done", "ticker": ticker, "pass": "B", "output_chars": len(pass_b_output)})

    return _assemble_tracker(
        ticker, report_date, is_stale, staleness_days, corpus_latest_date,
        pass_a_output, pass_b_output,
    )


def generate_strategic_analysis(summaries_list):
    """
    Generates a strategic analysis comparing performance vs expectations across quarters.
    summaries_list: List of dicts {'quarter': 'Q1', 'year': '2024', 'text': '...'}
    """
    context_str = ""
    for item in summaries_list:
        context_str += f"\n--- {item['quarter']} {item['year']} SUMMARY ---\n{item['text']}\n"

    prompt = """
    You are a Strategic Management Consultant for this company.

    **Goal:** Analyze the provided chronological earnings summaries to track the "Say-Do" ratio of management.
    Specifically, does the company achieve the goals and guidance it sets in one quarter when reported in the next?

    **Hard rules — non-negotiable:**
    - The summaries below are the source of truth. Every number, date, and quote must be traceable to them. Use `[not disclosed]` if a figure is absent — do not invent or back-fill from prior knowledge.
    - Quotes belong in quotation marks with a source tag like `[Source: Q3 2025 prepared remarks]`.
    - Each per-pair Verdict (Hit / Mixed / Miss) MUST go through the adversarial loop on attribution. The Executive Outlook Assessment at the top MUST be a synthesis of the per-pair loops, not an asserted opinion.

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
        return _call_claude(prompt + context_str)
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
        return _call_claude(prompt + text_snippet[:2000], model=FAST_CLASSIFIER_MODEL).strip()
    except Exception as e:
        log.error(f"Error identifying metadata: {e}")
        return "UNKNOWN"


def generate_event_brief(text: str) -> str:
    """
    Generate a structured brief for a non-quarterly IR event: investor day, AGM,
    capital markets day, conference deck, M&A announcement, ad-hoc strategic update.

    Events differ from quarterly artifacts: they are usually long-horizon strategy
    discussions (3-5 year targets, capital allocation philosophy, segment deep-dives,
    M&A rationale) rather than near-term financial results. Skip period numbers unless
    they materially shape the multi-year framework.
    """
    prompt = """You are a senior equity analyst summarizing an IR event document.

Events differ from quarterly artifacts: they are usually long-horizon strategy
discussions (3-5 year targets, capital allocation philosophy, segment deep-dives,
M&A rationale) rather than near-term financial results. Skip period numbers unless
they materially shape the multi-year framework.

**STRICT CONSTRAINT:** Start immediately with the title. No conversational filler.

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
[2-3 sentences on whether this strengthens, weakens, or is neutral to a long-term holder's thesis]

Event Document Text:
"""
    try:
        return _call_claude(prompt + text)
    except Exception as e:
        log.error(f"CRITICAL ERROR: Event brief generation failed: {e}")
        raise


def generate_bear_case(
    ticker: str,
    thesis: str,
    break_conditions: list[str],
    last_quarter_summaries: list[str],
    financials_table_md: str,
    segments_table_md: str,
    kpi_status_md: str,
) -> str:
    """
    Generate a structured bear case as a JSON string the caller parses.

    Schema: {failure_modes: list[FailureMode], most_underweighted: str,
    out_of_scope_flags: list[str]}.
    """
    transcripts_block = "\n\n".join(
        f"### Quarter {i + 1} (oldest first)\n{s[:6000]}" for i, s in enumerate(last_quarter_summaries)
    )

    prompt = f"""You are a senior fundamental equity analyst writing the bear case for {ticker}.
Be specific, quantified, and grounded ONLY in the data below. Do not fabricate
metrics or external events. If a real risk exists but the data here doesn't
support it, list it under `out_of_scope_flags` for manual review — do not invent
detail.

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

---

Produce a JSON object with EXACTLY these keys (no markdown, no commentary):

{{
  "failure_modes": [
    {{
      "hypothesis": "one-sentence concrete failure mode",
      "evidence_in_data": "which data point above supports it (cite a number or trend)",
      "leading_indicator": "what would confirm it next quarter",
      "quantitative_impact": "magnitude of revenue/margin/segment compression with reasoning chain",
      "refutation_criteria": "what mgmt would have to demonstrate over next 2-4Q to neutralize"
    }}
  ],
  "most_underweighted": "one paragraph: which failure mode is most underweighted by consensus and why",
  "out_of_scope_flags": ["risks real but not derivable from the inputs above (regulatory, macro)"]
}}

Provide 3 to 5 failure_modes. Return strictly the JSON object — nothing else.
"""
    try:
        raw = _call_claude(prompt).strip()
        # Strip ``` fences if Claude wraps the JSON despite the instruction.
        if raw.startswith("```"):
            raw = JSON_FENCE_RE.sub("", raw).strip()
        return raw
    except Exception as e:
        log.error(f"CRITICAL ERROR: Bear case generation failed for {ticker}: {e}")
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
        raw = _call_claude(prompt, model=FAST_CLASSIFIER_MODEL).strip()
        if raw.startswith("```"):
            raw = JSON_FENCE_RE.sub("", raw).strip()
        return json.loads(raw)
    except Exception as e:
        log.error(f"classify_intake_document failed: {e}")
        return None
