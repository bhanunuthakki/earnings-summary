"""
execution/run_pipeline.py
--------------------------
Layer 3 execution wrapper for the Earnings Transcript Processor.

Directive: directives/process_earnings_transcripts.md

Responsibilities:
  - Locate the project root (one level up from this file)
  - Validate that the virtualenv and .env are present
  - Delegate all real work to src/main.py via subprocess so
    the venv interpreter and PYTHONPATH are correctly set

Usage (run from project root):
    python execution/run_pipeline.py
"""

import os
import sys
import subprocess

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

VENV_PYTHON = os.path.join(PROJECT_ROOT, "venv", "Scripts", "python.exe")  # Windows
if not os.path.exists(VENV_PYTHON):
    # Fallback for Unix-style venvs
    VENV_PYTHON = os.path.join(PROJECT_ROOT, "venv", "bin", "python")

MAIN_SCRIPT = os.path.join(PROJECT_ROOT, "src", "main.py")
ENV_FILE    = os.path.join(PROJECT_ROOT, ".env")


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
def preflight():
    errors = []

    if not os.path.exists(VENV_PYTHON):
        errors.append(
            f"Virtualenv not found at expected path:\n  {VENV_PYTHON}\n"
            "  Run:  python -m venv venv && venv\\Scripts\\pip install -r requirements.txt"
        )

    if not os.path.exists(ENV_FILE):
        errors.append(
            f".env file not found at:\n  {ENV_FILE}\n"
            "  Create it with GEMINI_API_KEY=your_key_here"
        )

    if not os.path.exists(MAIN_SCRIPT):
        errors.append(f"Main script not found:\n  {MAIN_SCRIPT}")

    if errors:
        print("\n[run_pipeline] PRE-FLIGHT ERRORS:")
        for e in errors:
            print(f"  ✗ {e}")
        sys.exit(1)

    print("[run_pipeline] Pre-flight checks passed.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    preflight()

    env = os.environ.copy()
    # Make sure src/ modules are importable
    env["PYTHONPATH"] = os.path.join(PROJECT_ROOT, "src")

    print(f"[run_pipeline] Starting pipeline via: {VENV_PYTHON}")
    print(f"[run_pipeline] Script: {MAIN_SCRIPT}\n")
    print("=" * 60)

    result = subprocess.run(
        [VENV_PYTHON, MAIN_SCRIPT],
        cwd=PROJECT_ROOT,
        env=env,
    )

    print("=" * 60)
    if result.returncode == 0:
        print("[run_pipeline] Pipeline completed successfully.")
    else:
        print(f"[run_pipeline] Pipeline FAILED (exit code {result.returncode}).")
        sys.exit(result.returncode)


if __name__ == "__main__":
    main()
