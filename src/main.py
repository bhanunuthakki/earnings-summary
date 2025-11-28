import os
import shutil
import time
import sys
from parser import parse_filename, extract_text_from_pdf
from llm_client import generate_summary
from pdf_builder import create_cover_page, create_summary_pdf
from pdf_manager import build_final_master, merge_pdfs

import os
import shutil
import time
import sys
import re
from parser import parse_filename, extract_text_from_pdf
from llm_client import generate_summary
from pdf_builder import create_cover_page, create_summary_pdf, create_master_toc
from pdf_manager import build_final_master, merge_pdfs
from pypdf import PdfReader

INPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'transcripts_in')
PROCESSED_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'transcripts_processed')
MASTER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'transcripts_master')
TEMP_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'temp')
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'cache')

def main():
    print("Starting Earnings Transcript Processor (TOC Mode)...")
    
    # Ensure directories exist
    for d in [INPUT_DIR, PROCESSED_DIR, MASTER_DIR, TEMP_DIR, CACHE_DIR]:
        if not os.path.exists(d):
            os.makedirs(d)

    # 1. Ingest New Files
    new_files = [f for f in os.listdir(INPUT_DIR) if f.lower().endswith('.pdf')]
    if new_files:
        print(f"Found {len(new_files)} new files. Moving to archive...")
        for f in new_files:
            src = os.path.join(INPUT_DIR, f)
            dst = os.path.join(PROCESSED_DIR, f)
            if os.path.exists(dst):
                os.remove(dst)
            shutil.move(src, dst)

    # 2. Clear Output
    for f in os.listdir(MASTER_DIR):
        if f.lower().endswith('.pdf'):
            os.remove(os.path.join(MASTER_DIR, f))

    # 3. Scan Archive
    all_files = [f for f in os.listdir(PROCESSED_DIR) if f.lower().endswith('.pdf')]
    if not all_files:
        print("No transcripts found. Exiting.")
        return

    # 4. Sort
    parsed_files = []
    for f in all_files:
        try:
            meta = parse_filename(f)
            parsed_files.append({'filename': f, 'meta': meta})
        except ValueError:
            print(f"Skipping: {f}")
    
    # Sort by Company, Year, Quarter
    parsed_files.sort(key=lambda x: (x['meta']['company'], x['meta']['year'], x['meta']['quarter']))
    
    # Group by Company
    companies = {}
    for item in parsed_files:
        c = item['meta']['company']
        if c not in companies:
            companies[c] = []
        companies[c].append(item)

    # 5. Process per Company
    for company, items in companies.items():
        print(f"\nBuilding Master PDF for {company}...")
        
        toc_entries = []
        current_page_count = 0
        company_content_parts = []
        
        for item in items:
            filename = item['filename']
            meta = item['meta']
            filepath = os.path.join(PROCESSED_DIR, filename)
            
            print(f"  Processing {filename}...")
            
            try:
                quarter = meta['quarter']
                year = meta['year']
                
                # Cache/Gen Summary
                cache_path = os.path.join(CACHE_DIR, f"{company}_{quarter}_{year}_summary.txt")
                summary_text = ""
                
                if os.path.exists(cache_path):
                    print("    Loading summary from cache...")
                    with open(cache_path, 'r', encoding='utf-8') as f:
                        summary_text = f.read()
                else:
                    print("    Generating summary...")
                    text = extract_text_from_pdf(filepath)
                    summary_text = generate_summary(text)
                    with open(cache_path, 'w', encoding='utf-8') as f:
                        f.write(summary_text)
                    time.sleep(30)

                # Create Assets
                cover_path = os.path.join(TEMP_DIR, f"{company}_{quarter}_{year}_cover.pdf")
                summary_path = os.path.join(TEMP_DIR, f"{company}_{quarter}_{year}_summary.pdf")
                
                create_cover_page(cover_path, company, quarter, year)
                create_summary_pdf(summary_path, summary_text)
                
                # Calculate Pages
                # We need to know how many pages this section adds
                # Section = Cover + Summary + Transcript
                
                # Get page counts
                p_cover = len(PdfReader(cover_path).pages)
                p_summary = len(PdfReader(summary_path).pages)
                p_trans = len(PdfReader(filepath).pages)
                
                total_section_pages = p_cover + p_summary + p_trans
                
                # Record TOC Entry
                # Start page is current_page_count + 1 (1-based for humans)
                toc_entries.append({
                    'quarter': quarter,
                    'year': year,
                    'start_page': current_page_count, # 0-based index for bookmarking
                    'page': current_page_count + 1 # 1-based for display
                })
                
                current_page_count += total_section_pages
                
                # Add to list
                company_content_parts.extend([cover_path, summary_path, filepath])
                
            except Exception as e:
                print(f"CRITICAL ERROR on {filename}: {e}")
                sys.exit(1)

        # Build Final Master for this Company
        print(f"  Assembling final PDF for {company}...")
        
        # Merge all content first
        content_path = os.path.join(TEMP_DIR, f"{company}_content.pdf")
        merge_pdfs(content_path, company_content_parts)
        
        # Create TOC
        toc_path = os.path.join(TEMP_DIR, f"{company}_toc.pdf")
        create_master_toc(toc_path, company, toc_entries)
        
        # Final Merge
        master_path = os.path.join(MASTER_DIR, f"{company}_Master_Transcripts.pdf")
        build_final_master(master_path, toc_path, content_path, toc_entries)
        
        print(f"  SUCCESS: Created {master_path}")

if __name__ == '__main__':
    main()
