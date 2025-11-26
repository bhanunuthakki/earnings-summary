import os
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

def generate_summary(text):
    """
    Generates a 1-2 page summary of the earnings transcript using Gemini 1.5 Pro.
    """
    model = genai.GenerativeModel('gemini-2.5-pro')
    
    prompt = """
    You are an expert financial analyst. Please provide a detailed 1-2 page summary of the following earnings call transcript.
    
    Structure the summary as follows:
    1.  **Executive Summary**: High-level overview of performance and key takeaways.
    2.  **Financial Highlights**: Key metrics (Revenue, EPS, Margins, Guidance).
    3.  **Operational Highlights**: Key product updates, regional performance, etc.
    4.  **Q&A Key Points**: Important questions asked by analysts and management's responses.
    5.  **Outlook**: Future guidance and management sentiment.
    
    Keep the tone professional and concise.
    
    Transcript:
    """
    
    try:
        response = model.generate_content(prompt + text)
        return response.text
    except Exception as e:
        print(f"CRITICAL ERROR: Summary generation failed for the following reason:\n{e}")
        raise e # Re-raise to stop execution in main
