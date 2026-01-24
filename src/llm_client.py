import os
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))


def generate_pairwise_analysis(prev_summary, curr_summary):
    """
    Generates a specific "Say-Do" analysis comparing two sequential quarters.
    """
    model = genai.GenerativeModel('gemini-flash-latest')

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
    
    ### 1. Say (The Promise)
    *   **Guidance:** [Specific numbers/targets from {prev_q_str}]
    *   **Strategy:** [Key initiatives promised]

    ### 2. Do (The Reality)
    *   **Performance:** [Actuals in {curr_q_str}]
    *   **Gap Analysis:** [Specific variances]

    ### 3. Analyst Verdict
    *   **Performance Rating:** **MET** / **MISSED** / **EXCEEDED** (Choose one)
    *   **Attribution:** [Execution vs. Exogenous explanation]
    *   **Thesis View:** [Bull/Bear implication]
    """
    
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        print(f"Error generating pairwise analysis: {e}")
        return f"Could not generate analysis for {prev_q_str} -> {curr_q_str}."

def generate_summary(text):
    # ... existing code ...

    """
    Generates a 1-2 page summary of the earnings transcript.
    """
    model = genai.GenerativeModel('gemini-flash-latest')
    
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
        response = model.generate_content(prompt + text)
        return response.text
    except Exception as e:
        print(f"CRITICAL ERROR: Summary generation failed for the following reason:\n{e}")
        raise e # Re-raise to stop execution in main

def generate_strategic_analysis(summaries_list):
    """
    Generates a strategic analysis comparing performance vs expectations across quarters.
    summaries_list: List of dicts {'quarter': 'Q1', 'year': '2024', 'text': '...'}
    """
    model = genai.GenerativeModel('gemini-flash-latest')
    
    # Construct the input context
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
        response = model.generate_content(prompt + context_str)
        return response.text
    except Exception as e:
        print(f"CRITICAL ERROR: Analysis generation failed:\n{e}")
        raise e

def identify_transcript_metadata(text_snippet):
    """
    Identifies the Company Ticker, Quarter, and Year from the transcript text.
    """
    model = genai.GenerativeModel('gemini-flash-latest')
    
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
        response = model.generate_content(prompt + text_snippet[:2000]) # First 2000 chars should be enough
        return response.text.strip()
    except Exception as e:
        print(f"Error identifying metadata: {e}")
        return "UNKNOWN"


