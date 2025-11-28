import os
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

def generate_summary(text):
    """
    Generates a 1-2 page summary of the earnings transcript.
    """
    model = genai.GenerativeModel('gemini-2.5-pro')
    
    prompt = """
    You are an expert financial analyst. Please provide a detailed 1-2 page summary of the provided earnings call transcript.

**STRICT CONSTRAINT:** Do not provide conversational filler (e.g., "Here is the summary," "Sure, I can help"). Start your response immediately with the Report Title.

**Structure the summary as follows:**

1. **Executive Summary**

   * Provide a high-level overview of performance.

   * **Requirement:** Organize the rest of this section specifically by **Business Segment**, highlighting the narrative for each division.

2. **Financial Highlights**

   * **Table:** Create a Markdown table (renderable to PDF) with **Segments as Columns**.

     * Rows must include: Revenue, Operating Income/Margin, and QoQ and YoY Growth Rates for each metric when noted in the call.

   * **Context:** Below the table, provide text analysis of the key drivers and variances.

3. **Operational Highlights**

   * Key product updates, regional performance anomalies, and strategic initiatives.

4. **Q&A Key Points**

   * Summarize the most critical questions asked by analysts and management's direct responses (focus on contention points or strategic shifts).

5. **Outlook & Guidance**

   * **Quantitative:** List specific guidance figures provided (Revenue ranges, EPS targets, CapEx plans, Tax rates).

   * **Qualitative:** Summarize management’s sentiment and specific comments regarding future expectations, macro headwinds/tailwinds, and forward-looking strategy.

**Tone:** Professional, direct, and concise.
    
    Transcript:
    """
    
    try:
        response = model.generate_content(prompt + text)
        return response.text
    except Exception as e:
        print(f"CRITICAL ERROR: Summary generation failed for the following reason:\n{e}")
        raise e # Re-raise to stop execution in main
