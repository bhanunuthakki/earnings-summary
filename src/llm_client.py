"""
src/llm_client.py
-----------------
Prompt library for the project. Each function builds a domain-specific prompt
(summary, press release, presentation, SayDo, thesis tracker, metadata) and
delegates the actual LLM call to `llm_router.call_llm()`.

Provider routing — Claude CLI (subscription) primary, Gemini API fallback —
is handled in `src/llm_router.py`. This module does NOT import any LLM SDK
directly; everything goes through the router so future provider swaps are
single-file changes.
"""

from __future__ import annotations

import json

from llm_router import call_llm


# ---------------------------------------------------------------------------
# Pairwise SayDo: previous-quarter outlook vs current-quarter actuals
# ---------------------------------------------------------------------------


def generate_pairwise_analysis(prev_summary: dict, curr_summary: dict) -> str:
    """Strict Say-Do analysis between two consecutive quarter summaries."""
    prev_q_str = f"{prev_summary['quarter']} {prev_summary['year']}"
    curr_q_str = f"{curr_summary['quarter']} {curr_summary['year']}"

    prompt = f"""
    You are a Strategic Management Consultant and Senior Equity Analyst.
    **Task:** Perform a strict "Say-Do" analysis comparing the **Outlook/Guidance** from the Previous Quarter ({prev_q_str}) against the **Actual Results** reported in the Current Quarter ({curr_q_str}).

    **Input Data:**
    1.  **Previous Quarter ({prev_q_str}) Summary:**
        {prev_summary['text']}

    2.  **Current Quarter ({curr_q_str}) Summary:**
        {curr_summary['text']}

    **Analysis Requirements:**
    1.  **Context:** What did they promise? (Guidance, Targets, Strategic Goals).
    2.  **Execution:** What did they actually deliver? (Results, Misses, Beats).
    3.  **Analyst Verdict:**
        *   **Attribution:** Was any miss/beat due to **Execution** (Management Performance) or **Exogenous Factors** (Macro, Supply Chain, One-offs)?
        *   **Thesis Impact:** Is this a structural issue or a temporary blip?

    **Output Format (Strict Markdown):**
    ## Analysis: {prev_q_str} vs {curr_q_str}

    ### 1. Analyst Verdict
    *   **Performance Rating:** **MET** / **MISSED** / **EXCEEDED** (Choose one)
    *   **Attribution:** [Execution vs. Exogenous explanation]
    *   **Thesis View:** [Bull/Bear implication]

    ### 2. Say (The Promise)
    *   **Guidance:** [Specific numbers/targets from {prev_q_str}]
    *   **Strategy:** [Key initiatives promised]

    ### 3. Do (The Reality)
    *   **Performance:** [Actuals in {curr_q_str}]
    *   **Gap Analysis:** [Specific variances]
    """
    try:
        return call_llm(prompt)
    except Exception as e:
        print(f"Error generating pairwise analysis: {e}")
        return f"Could not generate analysis for {prev_q_str} -> {curr_q_str}."


# ---------------------------------------------------------------------------
# Per-quarter transcript summary
# ---------------------------------------------------------------------------


def generate_summary(text: str) -> str:
    """1-2 page structured summary of an earnings call transcript."""
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
        return call_llm(prompt + text)
    except Exception as e:
        print(f"CRITICAL ERROR: Summary generation failed for the following reason:\n{e}")
        raise


# ---------------------------------------------------------------------------
# Press release summary
# ---------------------------------------------------------------------------


def generate_press_release_summary(text: str) -> str:
    """Structured summary from an earnings press release (numbers + guidance)."""
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
[List 3-6 company-specific KPIs disclosed in the release (e.g. DAUs, GMV, RPO, cloud revenue, etc.)]

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
        return call_llm(prompt + text)
    except Exception as e:
        print(f"CRITICAL ERROR: Press release summary generation failed:\n{e}")
        raise


# ---------------------------------------------------------------------------
# Presentation deck brief
# ---------------------------------------------------------------------------


def generate_presentation_brief(text: str) -> str:
    """Strategic brief from an earnings presentation slide deck."""
    prompt = """
You are a senior equity research analyst. The following text was extracted from an earnings presentation slide deck.
Extract the key strategic narrative — what story is management telling investors?

**STRICT CONSTRAINT:** Do not provide conversational filler. Start your response immediately with the Report Title.

**Output Format (Strict Markdown):**

# Presentation Brief: [Company Ticker] [Quarter] [Year]

## 1. Management Narrative
[2-3 sentences on the central investor story management is presenting this quarter]

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
        return call_llm(prompt + text)
    except Exception as e:
        print(f"CRITICAL ERROR: Presentation brief generation failed:\n{e}")
        raise


# ---------------------------------------------------------------------------
# Micro-thesis tracker update
# ---------------------------------------------------------------------------


def generate_thesis_update(ticker: str, schema: dict, quarters: list[dict]) -> str:
    """
    Generate an updated micro-thesis tracker document for a holding.

    Args:
        ticker:   Company ticker (e.g. "GOOG")
        schema:   Holdings JSON schema from micro_thesis/holdings/<TICKER>.json
        quarters: List of {year, quarter, summaries: {doc_type: text}} dicts,
                  chronological order
    Returns:
        Markdown thesis tracker document.
    """
    thesis_text = json.dumps(schema, indent=2)

    quarter_blocks = []
    for q in quarters:
        block = f"\n### {q['quarter']} {q['year']}\n"
        for doc_type, text in q["summaries"].items():
            label = {
                "transcript": "Transcript Summary",
                "press_release": "Press Release Summary",
                "presentation": "Presentation Brief",
            }.get(doc_type, doc_type)
            block += f"\n**{label}:**\n{text[:3000]}\n"  # Cap per-doc to 3k chars
        quarter_blocks.append(block)
    quarters_context = "\n".join(quarter_blocks)

    prompt = f"""You are a senior fundamental equity analyst tracking a concentrated long position.

**Holding:** {ticker}
**Thesis & KPI Schema:**
{thesis_text}

**Available Evidence (last {len(quarters)} quarters, chronological):**
{quarters_context}

---

**Task:** Produce an updated micro-thesis tracker report. Be rigorous, concise, and analytically honest.

**Output Format (Strict Markdown):**

# Micro-Thesis Tracker: {ticker} — {{DATE}}

## Thesis Status: 🟢 INTACT / 🟡 MONITORING / 🔴 UNDER PRESSURE
[One sentence verdict]

## Tier 1 KPI Scorecard
| KPI | Latest Value | Trend | vs. Break Threshold | Status |
| :--- | :--- | :--- | :--- | :--- |
[For each tier_1_kpi in the schema: fill in latest known value, direction (up/down/flat), compare to the break condition, flag status]

## Key Developments This Period
[3-5 bullet points on material changes — new products, macro shifts, competitive moves, management credibility events]

## Say-Do Assessment
[Did management deliver on prior-quarter commitments? Rate: MET / MIXED / MISSED. Cite specific promises vs. actuals.]

## Thesis Breaker Watchlist
| Breaker | Status |
| :--- | :--- |
[For each thesis_breakers_qualitative: current status — Active Risk / Monitoring / Cleared]

## Competitive Watchlist Update
[Any material developments from the competitive_watchlist since last tracker]

## Open Questions for Next Quarter
[2-3 specific things to listen for / look for in next earnings]

## Analyst Notes
[Any asymmetries, positioning thoughts, or thesis evolution observations]
"""
    try:
        return call_llm(prompt)
    except Exception as e:
        print(f"CRITICAL ERROR: Thesis update generation failed for {ticker}:\n{e}")
        raise


# ---------------------------------------------------------------------------
# Strategic analysis — multi-quarter Say-Do over a window
# ---------------------------------------------------------------------------


def generate_strategic_analysis(summaries_list: list[dict]) -> str:
    """Multi-quarter Say-Do narrative across a chronological window."""
    context_str = ""
    for item in summaries_list:
        context_str += f"\n--- {item['quarter']} {item['year']} SUMMARY ---\n{item['text']}\n"

    prompt = """
    You are a Strategic Management Consultant for this company.

    **Goal:** Analyze the provided chronological earnings summaries to track the "Say-Do" ratio of management.
    Specifically, does the company achieve the goals and guidance it sets in one quarter when reported in the next?

    **Input:** A sequence of earnings call summaries.

    **Output Structure:**

    # Strategic Performance Analysis

    ## Executive Outlook Assessment
    Provide a high-level verdict: Is management credible? Do they consistently beat, meet, or miss their own expectations?

    ## Quarter-by-Quarter Track Record

    (Iterate through the timeline, comparing Q(N) Outlook to Q(N+1) Results)

    ### [Quarter N] Guidance vs [Quarter N+1] Reality
    *   **Expectation:** What did they promise in [Quarter N] (Outlook/Guidance)?
    *   **Reality:** What actually happened in [Quarter N+1]?
    *   **Verdict:** [Hit / Miss / Mixed] - Briefly explain why.

    ## Key Strategic Shifts
    Identify any major changes in strategy/narrative that occurred over this period.

    **Tone:** Analytical, objective, and critical where necessary.
    """
    try:
        return call_llm(prompt + context_str)
    except Exception as e:
        print(f"CRITICAL ERROR: Analysis generation failed:\n{e}")
        raise


# ---------------------------------------------------------------------------
# Filename / metadata extraction (used by smart_rename in src/parser.py)
# ---------------------------------------------------------------------------


def identify_transcript_metadata(text_snippet: str) -> str:
    """Identify (TICKER, Quarter, Year) from a transcript header. Returns
    `TICKER_QX_YYYY` or `UNKNOWN`."""
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
        return call_llm(prompt + text_snippet[:2000]).strip()
    except Exception as e:
        print(f"Error identifying metadata: {e}")
        return "UNKNOWN"
