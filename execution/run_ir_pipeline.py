"""
execution/run_ir_pipeline.py
------------------------------
Layer 2 orchestrator for the IR document ingestion pipeline.

For a given ticker (or all tickers), runs:
  0. categorize_ir_uploads.py — pick up any manually-dropped PDFs/XLSX at the
                                root of ir_documents/, classify them, move them
                                into the canonical ticker/period folders, and
                                register them in the documents table. No-op if
                                nothing is uncategorized.
  1. fetch_ir_documents.py    — download PDFs from discovered URL manifests
  2. process_ir_documents.py  — LLM-process each downloaded document
  3. update_thesis_tracker.py — refresh micro-thesis tracker

Step 0 is the manual-upload entry point and is the reason the IR pipeline as a
whole is *optional* on the broader project — when no IR files exist for a
ticker (no URL manifest, no manual uploads), the IR steps no-op cleanly and
the rest of the analysis runs without IR data.

Usage:
    python execution/run_ir_pipeline.py --ticker GOOG
    python execution/run_ir_pipeline.py --all
    python execution/run_ir_pipeline.py --ticker GOOG --skip-download   # re-process only
    python execution/run_ir_pipeline.py --ticker GOOG --skip-process    # download only
    python execution/run_ir_pipeline.py --skip-categorize  # opt out of upload triage
"""

import os
import sys
import argparse
import subprocess
import json
import logging
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"

VENV_PYTHON = PROJECT_ROOT / "venv" / "Scripts" / "python.exe"
if not VENV_PYTHON.exists():
    VENV_PYTHON = PROJECT_ROOT / "venv" / "bin" / "python"

URL_MANIFEST_DIR = PROJECT_ROOT / ".tmp" / "ir_url_manifest"

LOG_FORMAT = json.dumps({"level": "%(levelname)s", "ts": "%(asctime)s", "msg": "%(message)s"})
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stderr)
log = logging.getLogger(__name__)

MAX_RETRIES = 3


def run_step(step_name: str, cmd: list[str], allow_failure: bool = False) -> bool:
    """Run a subprocess step. Returns True on success."""
    log.info({"event": "step_start", "step": step_name, "cmd": " ".join(str(c) for c in cmd)})
    for attempt in range(1, MAX_RETRIES + 1):
        result = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
        if result.returncode == 0:
            log.info({"event": "step_done", "step": step_name})
            return True
        log.warning({"event": "step_failed", "step": step_name, "attempt": attempt, "exit_code": result.returncode})
        if attempt == MAX_RETRIES:
            msg = f"[run_ir_pipeline] Step '{step_name}' failed after {MAX_RETRIES} attempts."
            if allow_failure:
                print(f"WARNING: {msg}", file=sys.stderr)
                return False
            print(f"ERROR: {msg}", file=sys.stderr)
            sys.exit(1)
    return False


def preflight() -> None:
    errors = []
    if not VENV_PYTHON.exists():
        errors.append(f"Virtualenv Python not found: {VENV_PYTHON}")
    if errors:
        print("[run_ir_pipeline] PRE-FLIGHT ERRORS:")
        for e in errors:
            print(f"  ✗ {e}")
        sys.exit(1)


IR_DIR = PROJECT_ROOT / "ir_documents"


def all_tickers_from_manifests() -> list[str]:
    manifests = list(URL_MANIFEST_DIR.glob("*_urls.json"))
    return sorted(p.stem.replace("_urls", "") for p in manifests)


def all_tickers_from_uploads() -> list[str]:
    """Tickers that already have categorized uploads on disk."""
    if not IR_DIR.exists():
        return []
    out: list[str] = []
    for child in IR_DIR.iterdir():
        if child.is_dir() and child.name not in {"_unsorted"} and not child.name.startswith("."):
            out.append(child.name)
    return sorted(out)


def run_categorize_step() -> None:
    """Step 0: triage manual uploads at the root of ir_documents/.

    Always safe to run — silently exits when nothing is uncategorized. The
    `categorize_ir_uploads` script returns exit code 0 when all files are
    categorized, 1 when some were rejected (sidecared in `_unsorted/`).
    Either way the run continues; rejected files just won't make it into the
    pipeline until the user repairs them.
    """
    py = str(VENV_PYTHON)
    run_step(
        "categorize_ir_uploads",
        [py, str(SCRIPT_DIR / "categorize_ir_uploads.py")],
        allow_failure=True,
    )


def run_for_ticker(
    ticker: str,
    skip_download: bool = False,
    skip_process: bool = False,
    skip_tracker: bool = False,
) -> None:
    py = str(VENV_PYTHON)

    if not skip_download:
        run_step(
            f"fetch_ir_documents [{ticker}]",
            [py, str(SCRIPT_DIR / "fetch_ir_documents.py"), "--ticker", ticker],
            allow_failure=True,  # No URL manifest is normal for upload-only tickers.
        )

    if not skip_process:
        run_step(
            f"process_ir_documents [{ticker}]",
            [py, str(SCRIPT_DIR / "process_ir_documents.py"), "--ticker", ticker],
        )

    if not skip_tracker:
        run_step(
            f"update_thesis_tracker [{ticker}]",
            [py, str(SCRIPT_DIR / "update_thesis_tracker.py"), "--ticker", ticker],
            allow_failure=True,  # No holdings schema = non-fatal
        )

    log.info({"event": "ticker_complete", "ticker": ticker})


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the full IR document ingestion pipeline.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", type=str, help="Company ticker to process (e.g. GOOG)")
    group.add_argument("--all", action="store_true", help="Process all tickers with IR data (uploads or URL manifests)")
    parser.add_argument("--skip-categorize", action="store_true", help="Skip the manual-upload categorization step (step 0)")
    parser.add_argument("--skip-download", action="store_true", help="Skip the PDF download step")
    parser.add_argument("--skip-process", action="store_true", help="Skip LLM processing step")
    parser.add_argument("--skip-tracker", action="store_true", help="Skip thesis tracker update")
    args = parser.parse_args()

    preflight()

    if not args.skip_categorize:
        run_categorize_step()

    if args.all:
        tickers = sorted(set(all_tickers_from_manifests()) | set(all_tickers_from_uploads()))
        if not tickers:
            print(
                "[run_ir_pipeline] No tickers found — no URL manifests, no upload folders.",
                file=sys.stderr,
            )
            sys.exit(0)
        print(f"[run_ir_pipeline] Running for {len(tickers)} tickers: {', '.join(tickers)}")
        for t in tickers:
            run_for_ticker(t, args.skip_download, args.skip_process, args.skip_tracker)
    else:
        run_for_ticker(args.ticker, args.skip_download, args.skip_process, args.skip_tracker)

    print("[run_ir_pipeline] All done.")


if __name__ == "__main__":
    main()
