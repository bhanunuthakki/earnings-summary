import os
import shutil
import subprocess
import time
import sys
import re
import json
import argparse
from parser import parse_filename, extract_text_from_pdf, smart_rename_files, read_text_file
from llm_client import generate_summary, generate_strategic_analysis, generate_pairwise_analysis
from pdf_builder import create_cover_page, create_summary_pdf, create_master_toc, create_analysis_pdf, create_transcript_pdf
from pdf_manager import build_final_master, merge_pdfs
from pypdf import PdfReader
import index_manager
from alias_manager import resolve_ticker

PROJECT_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _spawn_background_cache_refresh() -> None:
    """Detach a non-blocking FMP cache refresh; return immediately.

    The cacher (execution/refresh_cache.py) fast-exits if no ticker is stale
    under its tier cadence, so this is cheap on every invocation. The lockfile
    inside the cacher prevents duplicate concurrent runs. Manual refresh:
    `python execution/refresh_cache.py run --force`. Tier from FMP_TIER env.
    """
    if os.environ.get("EARNINGS_SUMMARY_SKIP_CACHE_REFRESH") == "1":
        return
    cmd = [
        sys.executable,
        os.path.join(PROJECT_ROOT_DIR, "execution", "refresh_cache.py"),
        "run",
        "--background",
    ]
    try:
        subprocess.run(cmd, check=False, timeout=5)
    except (subprocess.SubprocessError, OSError) as e:
        # Non-fatal: cacher failure must never block the user's transcript run.
        sys.stderr.write(f"[cache-refresh] could not spawn: {type(e).__name__}: {e}\n")

INPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'transcripts', 'raw')
PROCESSED_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'transcripts', 'processed')
MASTER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'transcripts', 'master')
TEMP_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), '.tmp')
CACHE_DIR = TEMP_DIR

def update_status(company, status, step, error=None):
    if not company: return
    if not os.path.exists(CACHE_DIR):
        try: os.makedirs(CACHE_DIR)
        except: pass
    status_file = os.path.join(CACHE_DIR, f"{company}_status.json")
    try:
        with open(status_file, 'w') as f:
            json.dump({"status": status, "step": step, "error": error}, f)
    except:
        pass

def transcribe_audio_file(filepath):
    """Transcribes an audio file and saves it as .txt, returns the txt path."""
    from faster_whisper import WhisperModel
    base_name = os.path.splitext(os.path.basename(filepath))[0]
    output_txt_path = os.path.join(INPUT_DIR, f"{base_name}.txt")
    
    print(f"Transcribing audio: {filepath}")
    model = WhisperModel("large-v3-turbo", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(filepath, beam_size=5)
    
    transcript_lines = []
    for segment in segments:
        transcript_lines.append(f"[{segment.start:.2f}s -> {segment.end:.2f}s] {segment.text}")
        
    with open(output_txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(transcript_lines))
        
    print(f"Transcription saved to {output_txt_path}")
    return output_txt_path

def main(target_company=None):
    if target_company:
        target_company = resolve_ticker(target_company)
        print(f"Starting Earnings Transcript Processor (Audio-First) for: {target_company}...")
    else:
        print("Starting Earnings Transcript Processor (Audio-First)...")

    _spawn_background_cache_refresh()

    for d in [INPUT_DIR, PROCESSED_DIR, MASTER_DIR, TEMP_DIR, CACHE_DIR]:
        if not os.path.exists(d):
            os.makedirs(d)

    # 0. Intelligent Auto-Renaming
    print("Pre-scanning for file renaming...")
    smart_rename_files(INPUT_DIR)

    # 1. Process Audio Files into Text
    audio_files = [f for f in os.listdir(INPUT_DIR) if f.lower().endswith(('.mp3', '.m4a', '.wav'))]
    for audio_f in audio_files:
        audio_path = os.path.join(INPUT_DIR, audio_f)
        # Parse ticker to update status
        try:
            meta = parse_filename(audio_f)
            update_status(meta['company'], "running", "Transcribing uploaded audio...")
        except:
            pass
        
        try:
            transcribe_audio_file(audio_path)
        except Exception as e:
            print(f"Failed to transcribe {audio_f}: {e}")
            
        # Move audio to processed to archive it (or we could delete it)
        shutil.move(audio_path, os.path.join(PROCESSED_DIR, audio_f))

    # 2. Ingest Text (and legacy PDF) Files
    new_files = [f for f in os.listdir(INPUT_DIR) if f.lower().endswith(('.txt', '.pdf'))]
    if new_files:
        print(f"Found {len(new_files)} new transcript files. Moving to archive...")
        for f in new_files:
            src = os.path.join(INPUT_DIR, f)
            dst = os.path.join(PROCESSED_DIR, f)
            if os.path.exists(dst):
                os.remove(dst)
            shutil.move(src, dst)

    # 3. Scan Archive
    all_files = [f for f in os.listdir(PROCESSED_DIR) if f.lower().endswith(('.txt', '.pdf'))]
    if not all_files:
        print("No transcripts found. Exiting.")
        return

    # 4. Sort and Parse
    parsed_files = []
    for f in all_files:
        try:
            meta = parse_filename(f)
            # Only process if we found a valid format
            if 'company' not in meta: continue
            
            parsed_files.append({'filename': f, 'meta': meta})
            
            existing_record = index_manager.has_transcript(meta['company'], meta['year'], meta['quarter'])
            
            has_qa = None
            if existing_record and 'has_qa' in existing_record and existing_record['has_qa'] is not None:
                has_qa = existing_record['has_qa']
            else:
                full_path = os.path.join(PROCESSED_DIR, f)
                summary_cache_path = os.path.join(CACHE_DIR, f"{meta['company']}_{meta['quarter']}_{meta['year']}_summary.txt")
                
                if os.path.exists(summary_cache_path):
                    try:
                        with open(summary_cache_path, 'r', encoding='utf-8') as sf:
                            summary_text = sf.read()
                            missing_patterns = [r'does not include.*Q&A', r'no Q&A section', r'Q&A.*not provided', r'Q&A.*N/A', r'Analyst Concerns:.*N/A']
                            if any(re.search(p, summary_text, re.IGNORECASE) for p in missing_patterns):
                                has_qa = False
                            else:
                                has_qa = True
                    except: pass
                
                if has_qa is None:
                    try:
                        text_content = read_text_file(full_path) if f.endswith('.txt') else extract_text_from_pdf(full_path)
                        qa_patterns = [r'question-and-answer session', r'question and answer session', r'q\s*&\s*a', r'questions\s*and\s*answers']
                        has_qa = any(re.search(p, text_content, re.IGNORECASE) for p in qa_patterns)
                    except:
                        has_qa = False

            index_manager.register_transcript(
                meta['company'], meta['year'], meta['quarter'],
                source="MANUAL_OR_AUDIO", filepath=f, has_qa=has_qa
            )
            
        except ValueError:
            print(f"Skipping: {f}")
    
    parsed_files.sort(key=lambda x: (x['meta']['company'], x['meta']['year'], x['meta']['quarter']))
    
    companies = {}
    for item in parsed_files:
        c = item['meta']['company']
        if c not in companies: companies[c] = []
        companies[c].append(item)

    if target_company:
        if target_company in companies:
            companies = {target_company: companies[target_company]}
        else:
            print(f"Target company {target_company} not found. Exiting.")
            return

    for c in companies:
        companies[c].sort(key=lambda x: (x['meta']['year'], x['meta']['quarter']), reverse=True)
        # Cap to 8 most recent
        companies[c] = companies[c][:8]
        companies[c].sort(key=lambda x: (x['meta']['year'], x['meta']['quarter']))

    # 5. Process per Company
    for company, items in companies.items():
        master_path = os.path.join(MASTER_DIR, f"{company}_Master_Transcripts.pdf")
        manifest_path = os.path.join(CACHE_DIR, f"{company}_manifest.json")
        
        current_files = {}
        for item in items:
            fp = os.path.join(PROCESSED_DIR, item['filename'])
            if os.path.exists(fp):
                current_files[item['filename']] = os.path.getmtime(fp)

        rebuild_needed = False
        if not os.path.exists(master_path) or not os.path.exists(manifest_path):
            rebuild_needed = True
        else:
            try:
                with open(manifest_path, 'r') as f:
                    manifest = json.load(f)
                cached_files = manifest.get('files', {})
                if set(cached_files.keys()) != set(current_files.keys()):
                    rebuild_needed = True
                else:
                    for fname, mtime in current_files.items():
                        if mtime > cached_files.get(fname, 0):
                            rebuild_needed = True
                            break
            except:
                rebuild_needed = True
            
        if not rebuild_needed:
            print(f"\nSkipping {company} (Up to date).")
            continue
                
        print(f"\nBuilding Master PDF for {company}...")
        
        toc_entries = []
        current_page_count = 0
        company_content_parts = []
        company_summaries = [] 

        for item in items:
            filename = item['filename']
            meta = item['meta']
            filepath = os.path.join(PROCESSED_DIR, filename)
            
            print(f"  Processing {filename}...")
            
            try:
                quarter = meta['quarter']
                year = meta['year']
                
                cache_path = os.path.join(CACHE_DIR, f"{company}_{quarter}_{year}_summary.txt")
                summary_text = ""
                
                if os.path.exists(cache_path):
                    with open(cache_path, 'r', encoding='utf-8') as f:
                        summary_text = f.read()
                else:
                    update_status(company, "running", f"Generating LLM Summary for Q{quarter} {year}...")
                    text = read_text_file(filepath) if filename.endswith('.txt') else extract_text_from_pdf(filepath)
                    summary_text = generate_summary(text)
                    with open(cache_path, 'w', encoding='utf-8') as f:
                        f.write(summary_text)
                    time.sleep(30) # Rate limit

                curr_summary = {'quarter': quarter, 'year': year, 'text': summary_text}

                cover_path = os.path.join(TEMP_DIR, f"{company}_{quarter}_{year}_cover.pdf")
                summary_path = os.path.join(TEMP_DIR, f"{company}_{quarter}_{year}_summary.pdf")
                
                create_cover_page(cover_path, company, quarter, year)
                create_summary_pdf(summary_path, summary_text)
                
                p_cover = len(PdfReader(cover_path).pages)
                p_summary = len(PdfReader(summary_path).pages)
                
                # Handle Transcript PDF creation
                transcript_pdf_path = filepath
                if filename.endswith('.txt'):
                    transcript_pdf_path = os.path.join(TEMP_DIR, f"{company}_{quarter}_{year}_transcript.pdf")
                    text = read_text_file(filepath)
                    create_transcript_pdf(transcript_pdf_path, text)
                
                p_trans = len(PdfReader(transcript_pdf_path).pages)
                p_analysis = 0
                analysis_pdf_path = None
                
                if len(company_summaries) > 0:
                    prev_summary = company_summaries[-1]
                    try:
                        pair_key = f"SayDo_{company}_{prev_summary['quarter']}_{prev_summary['year']}_{curr_summary['quarter']}_{curr_summary['year']}"
                        pair_cache_path = os.path.join(CACHE_DIR, f"{pair_key}.txt")
                        
                        if os.path.exists(pair_cache_path):
                            with open(pair_cache_path, 'r', encoding='utf-8') as f:
                                pair_text = f.read()
                        else:
                            update_status(company, "running", f"Generating pairwise strategy analysis: {prev_summary['quarter']} -> {curr_summary['quarter']}...")
                            pair_text = generate_pairwise_analysis(prev_summary, curr_summary)
                            with open(pair_cache_path, 'w', encoding='utf-8') as f:
                                f.write(pair_text)
                                
                        analysis_text = f"# Strategic Performance Analysis: {company}\n\n{pair_text}"
                        analysis_pdf_path = os.path.join(TEMP_DIR, f"{company}_{quarter}_{year}_strategic_analysis.pdf")
                        create_analysis_pdf(analysis_pdf_path, analysis_text)
                        
                        p_analysis = len(PdfReader(analysis_pdf_path).pages)
                    except Exception as e:
                        print(f"  WARNING: Strategic Analysis failed for {quarter} {year} (skipping): {e}")

                company_summaries.append(curr_summary)
                
                total_section_pages = p_cover + p_summary + p_analysis + p_trans
                
                toc_entries.append({
                    'quarter': quarter, 'year': year,
                    'start_page': current_page_count, 'page': current_page_count + 1 
                })
                
                current_page_count += total_section_pages
                
                parts = [cover_path, summary_path]
                if analysis_pdf_path: parts.append(analysis_pdf_path)
                parts.append(transcript_pdf_path)
                company_content_parts.extend(parts)
                
            except Exception as e:
                print(f"CRITICAL ERROR on {filename}: {e}")
                sys.exit(1)

        print(f"  Assembling final PDF for {company}...")
        update_status(company, "running", f"Assembling final PDF for {company}...")
        
        content_path = os.path.join(TEMP_DIR, f"{company}_content.pdf")
        merge_pdfs(content_path, company_content_parts)
        
        toc_path = os.path.join(TEMP_DIR, f"{company}_toc.pdf")
        create_master_toc(toc_path, company, toc_entries)
        
        build_final_master(master_path, toc_path, content_path, toc_entries)
        
        try:
            with open(manifest_path, 'w') as f:
                json.dump({'company': company, 'files': current_files}, f, indent=2)
        except: pass
            
        for temp_file in os.listdir(TEMP_DIR):
            temp_path = os.path.join(TEMP_DIR, temp_file)
            if os.path.isfile(temp_path) and temp_file.lower().endswith('.pdf'):
                try: os.remove(temp_path)
                except: pass

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Process earnings transcripts")
    parser.add_argument("--company", type=str, help="Specific company ticker to process")
    args = parser.parse_args()
    main(target_company=args.company)
