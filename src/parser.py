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
import re
from llm_client import identify_transcript_metadata

def smart_rename_files(input_dir):
    """
    Scans the input directory for files without the standard naming convention.
    Renames them using LLM-inferred metadata.
    """
    print("Checking for files to rename...")
    for filename in os.listdir(input_dir):
        if not filename.lower().endswith('.pdf'):
            continue
            
        # Check if already matches format Company_Qx_YYYY.pdf
        # Regex: Any chars + _ + Q[1-4] + _ + 4 digits + .pdf
        match = re.match(r'(.+)_Q[1-4]_\d{4}\.pdf', filename)
        if match:
            # Only skip if the company part is a standard text ticker (no numbers/dots)
            # This forces re-evaluation of "2330" or "2330.TW"
            if match.group(1).isalpha():
                 continue
            
        print(f"  Inspecting unknown format: {filename}")
        filepath = os.path.join(input_dir, filename)

        # --- Heuristic Optimization ---
        # 0. Check for common patterns in filename BEFORE calling LLM
        # Patterns: "Company Q1 2025", "Company-Q1-2025", "Company_Q1 2025"
        # Regex groups: (Company) (Separator) (Q1-4) (Separator) (Year)
        heuristic_match = re.search(r'([a-zA-Z0-9\.]+)[ _-](Q[1-4])[ _-](20[2-3]\d)', filename, re.IGNORECASE)
        
        if heuristic_match:
            try:
                h_company = heuristic_match.group(1)
                h_quarter = heuristic_match.group(2).upper()
                h_year = heuristic_match.group(3)
                
                # Basic cleanup of company name if simple
                if h_company.lower() in ['nvda', 'tsm', 'goog', 'msft', 'aapl', 'meta', 'amzn']:
                    h_company = h_company.upper()
                
                # If we have a clean alphanumeric company name (or dot for Tickers), use it directly
                # If it looks like "nvidia-earnings", the LLM might be better to get "NVDA", 
                # but if the user named it "NVDA Q1 2025", we interpret strictly.
                if len(h_company) > 1:
                    new_filename = f"{h_company}_{h_quarter}_{h_year}.pdf"
                    print(f"    [Heuristic] Identified {h_company}, {h_quarter}, {h_year} from filename.")
                    
                    new_filepath = os.path.join(input_dir, new_filename)
                     # Renaming
                    if not os.path.exists(new_filepath):
                        print(f"    [Fast-Path] Renaming to: {new_filename}")
                        os.rename(filepath, new_filepath)
                        continue
                    else:
                        print(f"    Target {new_filename} already exists. Skipping.")
                        continue
            except Exception as e:
                print(f"    Heuristic check failed: {e}")
        # ------------------------------
        
        try:
            # Extract first few pages
            reader = PdfReader(filepath)
            text_snippet = ""
            for i in range(min(3, len(reader.pages))):
                text_snippet += reader.pages[i].extract_text() + "\n"
            
            # Identify
            metadata_str = identify_transcript_metadata(text_snippet)
            
            if metadata_str == "UNKNOWN" or "_" not in metadata_str:
                print(f"    Could not identify metadata for {filename}. Skipping.")
                continue
                
            # Rename
            new_filename = f"{metadata_str}.pdf"
            new_filepath = os.path.join(input_dir, new_filename)
            
            # Avoid overwrite if exists (maybe append counter if strictly needed, but simple for now)
            if os.path.exists(new_filepath):
                 print(f"    Target {new_filename} already exists. Skipping.")
                 continue

            print(f"    Renaming to: {new_filename}")
            try:
                os.rename(filepath, new_filepath)
            except Exception as e:
                print(f"    Error renaming: {e}")
            
        except Exception as e:
             print(f"    Error processing {filename}: {e}")
