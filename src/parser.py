import os
from pypdf import PdfReader
from alias_manager import resolve_ticker

def parse_filename(filename):
    """
    Parses the filename to extract Company, Quarter, and Year.
    Expected format: CompanyName_Quarter_Year.ext
    Example: Google_Q3_2024.txt
    """
    base_name = os.path.splitext(filename)[0]
    parts = base_name.split('_')
    
    if len(parts) < 3:
        raise ValueError(f"Filename {filename} does not match format Company_Quarter_Year")
    
    # Assuming the last two are Quarter and Year, and everything before is Company Name
    year = parts[-1]
    quarter = parts[-2]
    company = "_".join(parts[:-2])
    
    company = resolve_ticker(company)
    
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

def read_text_file(filepath):
    """Reads content from a text file."""
    with open(filepath, 'r', encoding='utf-8') as f:
        return f.read()

def smart_rename_files(input_dir):
    """
    Scans the input directory for files without the standard naming convention.
    Renames them using LLM-inferred metadata.
    """
    print("Checking for files to rename...")
    for filename in os.listdir(input_dir):
        ext = filename.lower().split('.')[-1]
        if ext not in ['pdf', 'txt', 'mp3', 'm4a', 'wav']:
            continue
            
        # Check if already matches format Company_Qx_YYYY.ext
        # Regex: Any chars + _ + Q[1-4] + _ + 4 digits + .ext
        match = re.match(r'(.+)_Q[1-4]_\d{4}\.(pdf|txt|mp3|m4a|wav)', filename.lower())
        if match:
            # Only skip if the company part is a standard text ticker (no numbers/dots)
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
                
                # Check for alias
                h_company = resolve_ticker(h_company)
                
                h_quarter = heuristic_match.group(2).upper()
                h_year = heuristic_match.group(3)
                
                # Basic cleanup of company name if simple
                if h_company.lower() in ['nvda', 'tsm', 'goog', 'msft', 'aapl', 'meta', 'amzn']:
                    h_company = h_company.upper()
                # Actually, resolve_ticker handles upper-casing, so the above line is semi-redundant 
                # but harmless.
                
                # If we have a clean alphanumeric company name (or dot for Tickers), use it directly
                # If it looks like "nvidia-earnings", the LLM might be better to get "NVDA", 
                # but if the user named it "NVDA Q1 2025", we interpret strictly.
                if len(h_company) > 1:
                    new_filename = f"{h_company}_{h_quarter}_{h_year}.{ext}"
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
        
        # If heuristic fails, try LLM (only works for text/pdf)
        if ext in ['pdf', 'txt']:
            try:
                if ext == 'pdf':
                    # Extract first few pages
                    reader = PdfReader(filepath)
                    text_snippet = ""
                    for i in range(min(3, len(reader.pages))):
                        text_snippet += reader.pages[i].extract_text() + "\n"
                else:
                    text_snippet = read_text_file(filepath)[:2000]
                
                # Identify
                metadata_str = identify_transcript_metadata(text_snippet)
                
                if metadata_str == "UNKNOWN" or "_" not in metadata_str:
                    print(f"    Could not identify metadata for {filename}. Skipping.")
                    continue
                    
                parts = metadata_str.split('_')
                ticker_part = "_".join(parts[:-2])
                quarter_part = parts[-2]
                year_part = parts[-1]
                metadata_str = f"{resolve_ticker(ticker_part)}_{quarter_part}_{year_part}"
                    
                # Rename
                new_filename = f"{metadata_str}.{ext}"
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
        else:
             print(f"    Cannot use LLM to rename audio file {filename}. Please rename manually.")
