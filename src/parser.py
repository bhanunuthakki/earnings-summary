import os
from pypdf import PdfReader

def parse_filename(filename):
    """
    Parses the filename to extract Company, Quarter, and Year.
    Expected format: CompanyName_Quarter_Year.pdf
    Example: Google_Q3_2024.pdf
    """
    base_name = os.path.splitext(filename)[0]
    parts = base_name.split('_')
    
    if len(parts) < 3:
        raise ValueError(f"Filename {filename} does not match format Company_Quarter_Year.pdf")
    
    # Assuming the last two are Quarter and Year, and everything before is Company Name
    year = parts[-1]
    quarter = parts[-2]
    company = "_".join(parts[:-2])
    
    return {
        'company': company,
        'quarter': quarter,
        'year': year
    }

def extract_text_from_pdf(filepath):
    """
    Extracts text from a PDF file.
    """
    reader = PdfReader(filepath)
    text = ""
    for page in reader.pages:
        text += page.extract_text() + "\n"
    return text
