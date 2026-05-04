"""
execution/run_pipeline.py
--------------------------
Layer 3 execution wrapper for the Earnings Transcript Processor.

Responsibilities:
  - Locate the project root
  - Determine the last 4 quarters to fetch based on current date
  - Call fetch_audio_transcripts.py for those quarters
  - Delegate all real work to src/main.py via subprocess

Usage:
    python execution/run_pipeline.py --company NVDA
"""

import os
import sys
import subprocess
import argparse
import json
import datetime

# Paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

VENV_PYTHON = os.path.join(PROJECT_ROOT, "venv", "Scripts", "python.exe")
if not os.path.exists(VENV_PYTHON):
    VENV_PYTHON = os.path.join(PROJECT_ROOT, "venv", "bin", "python")

MAIN_SCRIPT = os.path.join(PROJECT_ROOT, "src", "main.py")
ENV_FILE    = os.path.join(PROJECT_ROOT, ".env")
FETCH_SCRIPT = os.path.join(SCRIPT_DIR, "fetch_audio_transcripts.py")

def update_status(company, status, step, error=None):
    if not company: return
    cache_dir = os.path.join(PROJECT_ROOT, '.tmp')
    os.makedirs(cache_dir, exist_ok=True)
    status_file = os.path.join(cache_dir, f"{company}_status.json")
    try:
        with open(status_file, 'w') as f:
            json.dump({"status": status, "step": step, "error": error}, f)
    except:
        pass

def get_recent_quarters(num_quarters=4):
    """Returns a list of tuples (year, quarter) for the last N quarters."""
    quarters = []
    now = datetime.datetime.now()
    year = now.year
    # Determine current quarter based on month
    curr_quarter = (now.month - 1) // 3 + 1
    
    for _ in range(num_quarters):
        # We want to search for the *previous* quarter that just ended, or the current one if it's late in the quarter
        # For simplicity, we just walk backwards
        quarters.append((year, curr_quarter))
        curr_quarter -= 1
        if curr_quarter == 0:
            curr_quarter = 4
            year -= 1
    return quarters

def preflight():
    errors = []
    if not os.path.exists(VENV_PYTHON):
        errors.append(f"Virtualenv not found at expected path:\n  {VENV_PYTHON}")
    if not os.path.exists(ENV_FILE):
        errors.append(f".env file not found at:\n  {ENV_FILE}")
    if not os.path.exists(MAIN_SCRIPT):
        errors.append(f"Main script not found:\n  {MAIN_SCRIPT}")
    if not os.path.exists(FETCH_SCRIPT):
        errors.append(f"Fetch script not found:\n  {FETCH_SCRIPT}")
    
    if errors:
        print("\n[run_pipeline] PRE-FLIGHT ERRORS:")
        for e in errors:
            print(f"  X {e}")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Run the Audio Earnings Transcript pipeline.")
    parser.add_argument("--company", type=str, help="Specific company ticker to process")
    parser.add_argument("--source", type=str, default="youtube", help="Ignored, kept for compatibility")
    args = parser.parse_args()

    preflight()

    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.join(PROJECT_ROOT, "src")

    print(f"[run_pipeline] Starting audio pipeline via: {VENV_PYTHON}")
    if args.company:
        print(f"[run_pipeline] Target Company: {args.company}\n")
        
        # Fetch audio for the recent quarters
        recent_quarters = get_recent_quarters(2) # Just fetch the last 2 to save time/bandwidth initially
        for year, quarter in recent_quarters:
            update_status(args.company, "running", f"Fetching audio for Q{quarter} {year}...")
            print(f"[run_pipeline] Fetching Q{quarter} {year} audio for {args.company}...")
            
            fetch_cmd = [VENV_PYTHON, FETCH_SCRIPT, "--ticker", args.company, "--year", str(year), "--quarter", str(quarter)]
            fetch_result = subprocess.run(fetch_cmd, cwd=PROJECT_ROOT, env=env)
            if fetch_result.returncode != 0:
                print(f"[run_pipeline] Warning: fetch failed for Q{quarter} {year}. Moving on.")

    print(f"[run_pipeline] Processing transcripts via: {MAIN_SCRIPT}\n")
    update_status(args.company, "running", "Processing transcripts and generating insights...")

    cmd = [VENV_PYTHON, MAIN_SCRIPT]
    if args.company:
        cmd.extend(["--company", args.company])

    result = subprocess.run(cmd, cwd=PROJECT_ROOT, env=env)

    if result.returncode == 0:
        update_status(args.company, "completed", "Analysis completed successfully")
        print("[run_pipeline] Pipeline completed successfully.")
    else:
        update_status(args.company, "failed", "Pipeline failed", error=f"Pipeline FAILED (exit code {result.returncode})")
        sys.exit(result.returncode)

if __name__ == "__main__":
    main()
