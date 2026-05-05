import json
import os
import re

import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

# Empty-string ANTHROPIC_API_KEY would still trigger claude_cli's billing-guard.
if not os.environ.get("ANTHROPIC_API_KEY"):
    os.environ.pop("ANTHROPIC_API_KEY", None)

# Models
# `gemini-flash-latest` aliases to `gemini-3-flash`, capped at 20 RPD on the free tier.
# The intake classifier runs ~50 calls per migration batch, so it pins to 2.5-flash
# (1500 RPD free) for headroom. Summary/Say-Do functions stay on `gemini-flash-latest`.
INTAKE_CLASSIFIER_MODEL = "gemini-2.5-flash"

# Inbox classifier: keep the prompt below the 8 KB Gemini context budget for cheap calls
INTAKE_TEXT_BUDGET = 6000
JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)



def _complete(prompt: str) -> str:
    """Single LLM call — every public helper routes through this."""
    return call_claude(prompt)


def generate_pairwise_analysis(prev_summary, curr_summary):
    """Strict Say-Do analysis comparing prior-quarter guidance to current results."""
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

    return _complete(prompt)


def generate_summary(text):
    """Generate a 1-2 page summary of an earnings transcript."""
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

    return _complete(prompt + text)


def generate_press_release_summary(text: str) -> str:
    """Structured summary from an earnings press release."""
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

    return _complete(prompt + text)


def generate_presentation_brief(text: str) -> str:
    """Strategic brief from an earnings presentation slide deck."""
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
        response = model.generate_content(prompt + text)
        return response.text
    except Exception as e:
        print(f"CRITICAL ERROR: Presentation brief generation failed:\n{e}")
        raise e


def generate_event_brief(text: str) -> str:
    """
    Generate a structured brief for a non-quarterly IR event: investor day, AGM,
    capital markets day, conference deck, M&A announcement, ad-hoc strategic update.
    """
    model = genai.GenerativeModel("gemini-flash-latest")

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
        response = model.generate_content(prompt + text)
        return response.text
    except Exception as e:
        print(f"CRITICAL ERROR: Event brief generation failed:\n{e}")
        raise e


def generate_thesis_update(ticker: str, schema: dict, quarters: list[dict]) -> str:
    """
    Generate an updated micro-thesis tracker document for a holding.

    Args:
        ticker: Company ticker (e.g. "GOOG")
        schema: Holdings JSON schema from micro_thesis/holdings/<TICKER>.json
        quarters: List of {year, quarter, summaries: {doc_type: text}} dicts, chronological order


def generate_thesis_update(ticker: str, schema: dict, quarters: list[dict]) -> str:
    """Updated micro-thesis tracker document for a holding."""
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
            block += f"\n**{label}:**\n{text[:3000]}\n"
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
[For each tier_1_kpi in the schema: fill in latest known value, direction (↑↓→), compare to the break condition, flag 🟢/🟡/🔴]

## Key Developments This Period
[3–5 bullet points on material changes — new products, macro shifts, competitive moves, management credibility events]

## Say-Do Assessment
[Did management deliver on prior-quarter commitments? Rate: MET / MIXED / MISSED. Cite specific promises vs. actuals.]

## Thesis Breaker Watchlist
| Breaker | Status |
| :--- | :--- |
[For each thesis_breakers_qualitative: current status — Active Risk / Monitoring / Cleared]

## Competitive Watchlist Update
[Any material developments from the competitive_watchlist since last tracker]

## Open Questions for Next Quarter
[2–3 specific things to listen for / look for in next earnings]

## Analyst Notes
[Any asymmetries, positioning thoughts, or thesis evolution observations]
"""

    return _complete(prompt)


def generate_strategic_analysis(summaries_list):
    """Strategic analysis comparing performance vs expectations across quarters."""
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

    return _complete(prompt + context_str)


def generate_bear_case(
    ticker: str,
    thesis: str,
    break_conditions: list[str],
    last_quarter_summaries: list[str],
    financials_table_md: str,
    segments_table_md: str,
    kpi_status_md: str,
) -> str:
    """Generate a structured bear case as a JSON string the caller parses.

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

    return _complete(prompt)


def identify_transcript_metadata(text_snippet):
    """Identify Company Ticker, Quarter, and Year from the transcript text."""
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

    return _complete(prompt + text_snippet[:2000]).strip()
def classify_intake_document(filename: str, text: str, hint: dict) -> dict | None:
    """Classify a user-dropped IR document.

    Returns a dict with keys (ticker, period_end, doc_type, confidence, reasoning)
    matching `src.intake.IntakeClassification`, or None on any LLM/parse failure.
    """
    model = genai.GenerativeModel(INTAKE_CLASSIFIER_MODEL)

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
        response = model.generate_content(prompt)
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = JSON_FENCE_RE.sub("", raw).strip()
        return json.loads(raw)
    except Exception as e:
        print(f"classify_intake_document failed: {e}")
        return None

