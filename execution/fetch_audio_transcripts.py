"""
execution/fetch_audio_transcripts.py
--------------------------
Layer 3 execution script to fetch earnings call audio and transcribe locally.

Responsibilities:
  - Find earnings call audio on YouTube via yt-dlp.
  - Download as audio to .tmp/.
  - Transcribe using faster-whisper.
  - Save the transcript text to transcripts/raw/.
"""

import os
import sys
import argparse
import yt_dlp
from faster_whisper import WhisperModel
import json

# Paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
sys.path.append(SRC_DIR)

from alias_manager import resolve_ticker
import index_manager

INPUT_DIR = os.path.join(PROJECT_ROOT, "transcripts", "raw")
CACHE_DIR = os.path.join(PROJECT_ROOT, ".tmp")

def update_status(company, status, step, error=None):
    if not company: return
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR, exist_ok=True)
    status_file = os.path.join(CACHE_DIR, f"{company}_status.json")
    try:
        with open(status_file, 'w') as f:
            json.dump({"status": status, "step": step, "error": error}, f)
    except:
        pass

def fetch_and_transcribe(ticker, year, quarter):
    original_ticker = ticker.upper()
    ticker = resolve_ticker(original_ticker)
    
    str_quarter = f"Q{quarter}"
    expected_filename = f"{ticker}_{str_quarter}_{year}.txt"
    expected_filepath = os.path.join(INPUT_DIR, expected_filename)
    
    if index_manager.has_transcript(ticker, year, str_quarter) or os.path.exists(expected_filepath):
        print(f"Transcript already exists for {ticker} {str_quarter} {year}. Skipping.")
        return
        
    os.makedirs(INPUT_DIR, exist_ok=True)
    
    search_query = f"{original_ticker} {str_quarter} {year} earnings call audio full"
    
    print(f"\n[{ticker} {str_quarter} {year}] Searching YouTube: '{search_query}'")
    update_status(ticker, "running", f"Downloading {str_quarter} {year} audio from YouTube...")
    
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': os.path.join(PROJECT_ROOT, ".tmp", f"temp_audio_{ticker}_{str_quarter}_{year}.%(ext)s"),
        'default_search': 'ytsearch1',
        'quiet': False
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([search_query])
            
        # Find the actual downloaded file (since extension is dynamic)
        downloaded_file = None
        for f in os.listdir(os.path.join(PROJECT_ROOT, ".tmp")):
            if f.startswith(f"temp_audio_{ticker}_{str_quarter}_{year}."):
                downloaded_file = os.path.join(PROJECT_ROOT, ".tmp", f)
                break
                
        if not downloaded_file:
            raise Exception("Downloaded audio file not found in .tmp directory")
            
        temp_audio = downloaded_file

    except Exception as e:
        print(f"Failed to download audio: {e}")
        update_status(ticker, "failed", "Audio download failed", error=str(e))
        return
        
    print(f"[{ticker} {str_quarter} {year}] Transcribing audio locally using faster-whisper...")
    update_status(ticker, "running", f"Transcribing audio with Whisper...")
    
    try:
        # Load model. large-v3-turbo is recommended.
        model = WhisperModel("large-v3-turbo", device="cpu", compute_type="int8")
        segments, info = model.transcribe(temp_audio, beam_size=5)
        
        transcript_lines = []
        for segment in segments:
            # Just store raw text. We could keep timestamps if needed.
            transcript_lines.append(f"[{segment.start:.2f}s -> {segment.end:.2f}s] {segment.text}")
            
        with open(expected_filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(transcript_lines))
            
        print(f"[{ticker} {str_quarter} {year}] Transcription complete. Saved to {expected_filepath}")
        
        # Cleanup temp audio
        if os.path.exists(temp_audio):
            os.remove(temp_audio)
            
        index_manager.register_transcript(
            ticker,
            year,
            str_quarter,
            source="yt_dlp_whisper",
            filepath=expected_filename,
            has_qa=None # To be determined by LLM summary later
        )
            
    except Exception as e:
        print(f"Transcription failed: {e}")
        update_status(ticker, "failed", "Transcription failed", error=str(e))
        return

def main():
    parser = argparse.ArgumentParser(description="Fetch and Transcribe Earnings Call")
    parser.add_argument("--ticker", required=True, help="Stock ticker symbol (e.g. NVDA)")
    parser.add_argument("--year", required=True, type=int, help="Year (e.g. 2025)")
    parser.add_argument("--quarter", required=True, type=int, choices=[1, 2, 3, 4], help="Quarter (1-4)")
    args = parser.parse_args()
    
    fetch_and_transcribe(args.ticker, args.year, args.quarter)

if __name__ == "__main__":
    main()
