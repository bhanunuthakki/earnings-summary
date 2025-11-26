import os
import shutil
import time
import sys
from parser import parse_filename, extract_text_from_pdf
from llm_client import generate_summary
from pdf_builder import create_cover_page, create_summary_pdf
from pdf_manager import append_to_master

import os
import shutil
import time
import sys
import re
from parser import parse_filename, extract_text_from_pdf
from llm_client import generate_summary
from pdf_builder import create_cover_page, create_summary_pdf
from pdf_manager import append_to_master

INPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'transcripts_in')
PROCESSED_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'transcripts_processed')
MASTER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'transcripts_master')
TEMP_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'temp')
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'cache')

def main():
    print("Starting Earnings Transcript Processor (Cached & Rebuild Mode)...")
    
    # Ensure directories exist
    for d in [INPUT_DIR, PROCESSED_DIR, MASTER_DIR, TEMP_DIR, CACHE_DIR]:
        if not os.path.exists(d):
            os.makedirs(d)

    # 1. Ingest New Files: Move from INPUT_DIR to PROCESSED_DIR
    new_files = [f for f in os.listdir(INPUT_DIR) if f.lower().endswith('.pdf')]
    if new_files:
        print(f"Found {len(new_files)} new files in input. Moving to archive...")
        for f in new_files:
            src = os.path.join(INPUT_DIR, f)
            dst = os.path.join(PROCESSED_DIR, f)
            # Handle duplicates if file already exists in processed
            if os.path.exists(dst):
                print(f"  Warning: {f} already exists in archive. Overwriting.")
                os.remove(dst)
            shutil.move(src, dst)
    else:
        print("No new files in input directory.")

    # 2. Clear Existing Master PDFs (To rebuild from scratch)
    print("Clearing existing Master PDFs...")
    for f in os.listdir(MASTER_DIR):
        if f.lower().endswith('.pdf'):
            os.remove(os.path.join(MASTER_DIR, f))

    # 3. Scan ALL files in PROCESSED_DIR (The Archive)
    all_files = [f for f in os.listdir(PROCESSED_DIR) if f.lower().endswith('.pdf')]
    
    if not all_files:
        print(f"No transcripts found in archive ({PROCESSED_DIR}). Exiting.")
        return

    # 4. Sort files chronologically
    parsed_files = []
    for f in all_files:
        try:
            meta = parse_filename(f)
            parsed_files.append({'filename': f, 'meta': meta})
        except ValueError:
            print(f"Skipping file with invalid format: {f}")
    
    # Sort by Company, then Year, then Quarter
    parsed_files.sort(key=lambda x: (x['meta']['company'], x['meta']['year'], x['meta']['quarter']))
    
    print(f"Found {len(parsed_files)} total transcripts to process.")

    # 5. Process Each File
    for item in parsed_files:
        filename = item['filename']
        meta = item['meta']
        filepath = os.path.join(PROCESSED_DIR, filename)
        
        print(f"\nProcessing {filename}...")
        
        try:
            company = meta['company']
            quarter = meta['quarter']
            year = meta['year']
            
            # Cache Key
            cache_filename = f"{company}_{quarter}_{year}_summary.txt"
            cache_path = os.path.join(CACHE_DIR, cache_filename)
            
            summary_text = ""
            
            # Check Cache
            if os.path.exists(cache_path):
                print("  Found cached summary. Loading...")
                with open(cache_path, 'r', encoding='utf-8') as f:
                    summary_text = f.read()
            else:
                # Extract and Generate
                print("  Extracting text...")
                text = extract_text_from_pdf(filepath)
                
                print("  Generating summary (Gemini 2.5 Pro)...")
                summary_text = generate_summary(text)
                
                # Save to Cache
                with open(cache_path, 'w', encoding='utf-8') as f:
                    f.write(summary_text)
                print("  Saved summary to cache.")
                
                # Rate Limit only if we hit the API
                print("  Sleeping for 30s to respect API rate limits...")
                time.sleep(30)

            # Create Intermediate PDFs
            print("  Creating PDF assets...")
            cover_path = os.path.join(TEMP_DIR, "cover.pdf")
            summary_path = os.path.join(TEMP_DIR, "summary.pdf")
            
            create_cover_page(cover_path, company, quarter, year)
            create_summary_pdf(summary_path, summary_text)
            
            # Append to Master
            master_filename = f"{company}_Master_Transcripts.pdf"
            master_path = os.path.join(MASTER_DIR, master_filename)
            
            print(f"  Appending to {master_filename}...")
            append_to_master(master_path, [cover_path, summary_path, filepath])
            
        except Exception as e:
            print(f"\n!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            print(f"CRITICAL FAILURE processing {filename}")
            print(f"Error details: {e}")
            print(f"Stopping execution immediately.")
            print(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            sys.exit(1)

if __name__ == '__main__':
    main()
