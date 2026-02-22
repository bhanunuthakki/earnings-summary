import os
import shutil
import time
import sys
import re
import json
from parser import parse_filename, extract_text_from_pdf, smart_rename_files
from llm_client import generate_summary, generate_strategic_analysis, generate_pairwise_analysis
from pdf_builder import create_cover_page, create_summary_pdf, create_master_toc, create_analysis_pdf
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

    # 0. Intelligent Auto-Renaming
    print("Pre-scanning for file renaming...")
    smart_rename_files(INPUT_DIR)

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

    # 2. Clear Output - DISABLED for incremental build
    # for f in os.listdir(MASTER_DIR):
    #     if f.lower().endswith('.pdf'):
    #         os.remove(os.path.join(MASTER_DIR, f))

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
        master_path = os.path.join(MASTER_DIR, f"{company}_Master_Transcripts.pdf")
        manifest_path = os.path.join(CACHE_DIR, f"{company}_manifest.json")
        
        # Gather current file state
        current_files = {}
        for item in items:
            fp = os.path.join(PROCESSED_DIR, item['filename'])
            if os.path.exists(fp):
                current_files[item['filename']] = os.path.getmtime(fp)

        # Check if rebuild is needed
        rebuild_needed = False
        
        if not os.path.exists(master_path):
            rebuild_needed = True
        elif not os.path.exists(manifest_path):
            rebuild_needed = True
            print(f"  [Manifest] Missing manifest for {company}, triggering rebuild.")
        else:
            try:
                with open(manifest_path, 'r') as f:
                    manifest = json.load(f)
                cached_files = manifest.get('files', {})
                
                # Check 1: Set difference (Files added/removed/renamed)
                if set(cached_files.keys()) != set(current_files.keys()):
                    rebuild_needed = True
                    print(f"  [Manifest] File list changed for {company}.")
                else:
                    # Check 2: Timestamp comparison (Content modification)
                    for fname, mtime in current_files.items():
                        cached_mtime = cached_files.get(fname, 0)
                        # Use a small epsilon for float comparison if needed, or simple >
                        if mtime > cached_mtime: 
                            rebuild_needed = True
                            print(f"  [Manifest] File modified: {fname}")
                            break
            except Exception as e:
                print(f"  [Manifest] Error reading manifest: {e}. Rebuilding.")
                rebuild_needed = True
            
        if not rebuild_needed:
            print(f"\nSkipping {company} (Up to date).")
            continue
                
        print(f"\nBuilding Master PDF for {company}...")
        
        toc_entries = []
        current_page_count = 0
        company_content_parts = []
        
        # Collection for Strategic Analysis
        company_summaries = [] 

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

                # Store for analysis
                company_summaries.append({
                    'quarter': quarter,
                    'year': year,
                    'text': summary_text
                })

                # Create Assets
                cover_path = os.path.join(TEMP_DIR, f"{company}_{quarter}_{year}_cover.pdf")
                summary_path = os.path.join(TEMP_DIR, f"{company}_{quarter}_{year}_summary.pdf")
                
                create_cover_page(cover_path, company, quarter, year)
                create_summary_pdf(summary_path, summary_text)
                
                # Calculate Pages
                p_cover = len(PdfReader(cover_path).pages)
                p_summary = len(PdfReader(summary_path).pages)
                p_trans = len(PdfReader(filepath).pages)
                
                total_section_pages = p_cover + p_summary + p_trans
                
                # Record TOC Entry (Transcript Sections)
                toc_entries.append({
                    'quarter': quarter,
                    'year': year,
                    'start_page': current_page_count, 
                    'page': current_page_count + 1 
                })
                
                current_page_count += total_section_pages
                
                # Add to list
                company_content_parts.extend([cover_path, summary_path, filepath])
                
            except Exception as e:
                print(f"CRITICAL ERROR on {filename}: {e}")
                sys.exit(1)

        # --- STRATEGIC ANALYSIS PHASE ---
                # --- STRATEGIC ANALYSIS PHASE (PAIRWISE) ---
        if len(company_summaries) > 1:
            print(f"  Generating Strategic Analysis (Pairwise) for {len(company_summaries)} quarters...")
            analysis_text = f"# Strategic Performance Analysis: {company}\n\n"
            
            # Sort chronologically just in case (though file sorting typically handles this)
            # company_summaries is populated in loop order, which was sorted by filename keys.
            
            try:
                pairwise_results = []
                for i in range(1, len(company_summaries)):
                    prev = company_summaries[i-1]
                    curr = company_summaries[i]
                    
                    # Construct Pairwise Cache Key
                    # e.g. "SayDo_NVDA_Q1_2026_Q2_2026.txt"
                    pair_key = f"SayDo_{company}_{prev['quarter']}_{prev['year']}_{curr['quarter']}_{curr['year']}"
                    pair_cache_path = os.path.join(CACHE_DIR, f"{pair_key}.txt")
                    
                    pair_text = ""
                    
                    # 1. Check Cache for Pair
                    if os.path.exists(pair_cache_path):
                        print(f"    [Cache Hit] Loading analysis: {prev['quarter']} -> {curr['quarter']}")
                        with open(pair_cache_path, 'r', encoding='utf-8') as f:
                            pair_text = f.read()
                    else:
                        print(f"    [Gen AI] Analyzing: {prev['quarter']} -> {curr['quarter']}...")
                        pair_text = generate_pairwise_analysis(prev, curr)
                        with open(pair_cache_path, 'w', encoding='utf-8') as f:
                            f.write(pair_text)
                            
                    pairwise_results.append(pair_text)
             
                # Reverse for final output (Latest -> Oldest)
                for text in reversed(pairwise_results):
                    analysis_text += text + "\n\n" + ("-" * 40) + "\n\n"
             
                # 2. Create PDF with aggregated text
                analysis_pdf_path = os.path.join(TEMP_DIR, f"{company}_strategic_analysis.pdf")
                create_analysis_pdf(analysis_pdf_path, analysis_text)
                
                # 3. Insert into Content List (AT THE START)
                company_content_parts.insert(0, analysis_pdf_path)
                
                # 4. Adjust Page Counts & TOC
                # The analysis section shifts everything else down.
                p_analysis = len(PdfReader(analysis_pdf_path).pages)
                
                # Add Analysis to TOC as the first item
                analysis_toc_entry = {
                    'quarter': "Strategic",
                    'year': "Analysis",
                    'start_page': 0,
                    'page': 1
                }
                toc_entries.insert(0, analysis_toc_entry)
                
                # Shift all other TOC entries
                for entry in toc_entries[1:]:
                    entry['start_page'] += p_analysis
                    entry['page'] += p_analysis
                    
            except Exception as e:
                print(f"  WARNING: Strategic Analysis failed (skipping): {e}")

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
        
        # Update Manifest
        try:
            with open(manifest_path, 'w') as f:
                json.dump({'company': company, 'files': current_files}, f, indent=2)
            print(f"  [Manifest] Updated {manifest_path}")
        except Exception as e:
            print(f"  [Manifest] Warning: Could not save manifest: {e}")

if __name__ == '__main__':
    main()
