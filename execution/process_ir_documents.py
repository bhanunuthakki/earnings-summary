"""
execution/process_ir_documents.py
-----------------------------------
Layer 3 CLI: LLM-process downloaded IR documents (press_release, presentation, transcript).

For each unprocessed document registered in document_index.json:
  - press_release  → generate_press_release_summary()  → .tmp/<TICKER>_<Q>_<Y>_press_release_summary.txt
  - presentation   → generate_presentation_brief()      → .tmp/<TICKER>_<Q>_<Y>_presentation_brief.txt
  - transcript     → generate_summary()                 → .tmp/<TICKER>_<Q>_<Y>_summary.txt  (existing)

Respects the same rate-limit pause and cache idempotency as the existing transcript pipeline.

Usage:
    python execution/process_ir_documents.py --ticker GOOG
    python execution/process_ir_documents.py --ticker GOOG --quarter Q3 --year 2025
    python execution/process_ir_documents.py --all
    python execution/process_ir_documents.py --dry-run --all
"""

import os
import sys
import time
import json
import argparse
import logging
from pathlib import Path

# Paths
SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

import index_manager
from parser import extract_text_from_pdf, read_text_file
from llm_client import generate_summary, generate_press_release_summary, generate_presentation_brief
from alias_manager import resolve_ticker

CACHE_DIR = PROJECT_ROOT / ".tmp"

LOG_FORMAT = json.dumps({"level": "%(levelname)s", "ts": "%(asctime)s", "msg": "%(message)s"})
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stderr)
log = logging.getLogger(__name__)

# Seconds to sleep between LLM calls to avoid rate limiting
RATE_LIMIT_SLEEP = 15

DOC_TYPE_CONFIG: dict[str, dict] = {
    "press_release": {
        "cache_suffix": "press_release_summary.txt",
        "llm_fn": generate_press_release_summary,
        "label": "Press Release Summary",
    },
    "presentation": {
        "cache_suffix": "presentation_brief.txt",
        "llm_fn": generate_presentation_brief,
        "label": "Presentation Brief",
    },
    "transcript": {
        "cache_suffix": "summary.txt",
        "llm_fn": generate_summary,
        "label": "Transcript Summary",
    },
}


def cache_path_for(doc: dict) -> Path:
    ticker = doc["ticker"]
    quarter = doc["quarter"]
    year = doc["year"]
    suffix = DOC_TYPE_CONFIG[doc["doc_type"]]["cache_suffix"]
    return CACHE_DIR / f"{ticker}_{quarter}_{year}_{suffix}"


def extract_text(local_path: str) -> str:
    path = Path(local_path)
    if path.suffix.lower() == ".pdf":
        return extract_text_from_pdf(str(path))
    return read_text_file(str(path))


def process_document(doc: dict, dry_run: bool = False) -> bool:
    """
    Process a single document entry. Returns True if processed successfully.
    Idempotent: skips if cache already exists.
    """
    ticker = doc["ticker"]
    quarter = doc["quarter"]
    year = doc["year"]
    doc_type = doc["doc_type"]
    local_path = doc.get("local_path")

    config = DOC_TYPE_CONFIG.get(doc_type)
    if config is None:
        log.warning({"event": "unknown_doc_type", "doc_type": doc_type, "ticker": ticker})
        return False

    if not local_path or not Path(local_path).exists():
        log.warning({"event": "local_path_missing", "ticker": ticker, "quarter": quarter, "year": year, "doc_type": doc_type})
        return False

    cache = cache_path_for(doc)

    if cache.exists():
        log.info({"event": "cache_hit", "cache": str(cache)})
        # Still mark as processed in the index if not already
        index_manager.mark_document_processed(ticker, year, quarter, doc_type)
        return True

    label = config["label"]
    log.info({"event": "processing", "ticker": ticker, "quarter": quarter, "year": year, "doc_type": doc_type, "label": label})

    if dry_run:
        print(f"[DRY RUN] Would process: {ticker} {quarter} {year} {doc_type} -> {cache.name}")
        return True

    try:
        text = extract_text(local_path)
        if not text.strip():
            log.error({"event": "empty_text", "local_path": local_path})
            return False

        llm_fn = config["llm_fn"]
        result_text = llm_fn(text)

        with open(cache, "w", encoding="utf-8") as f:
            f.write(result_text)

        log.info({"event": "cached", "cache": str(cache)})
        index_manager.mark_document_processed(ticker, year, quarter, doc_type)
        time.sleep(RATE_LIMIT_SLEEP)
        return True

    except Exception as e:
        log.error({"event": "processing_failed", "ticker": ticker, "quarter": quarter, "year": year, "doc_type": doc_type, "error": str(e)})
        return False


def run_for_ticker(
    ticker: str,
    quarter: str | None = None,
    year: str | None = None,
    dry_run: bool = False,
) -> None:
    ticker = resolve_ticker(ticker).upper()
    docs = index_manager.get_unprocessed_documents(ticker)

    if quarter:
        docs = [d for d in docs if d["quarter"].upper() == quarter.upper()]
    if year:
        docs = [d for d in docs if str(d["year"]) == str(year)]

    if not docs:
        print(f"[{ticker}] No unprocessed documents found.")
        return

    print(f"[{ticker}] Processing {len(docs)} document(s)...")
    success = 0
    for doc in docs:
        ok = process_document(doc, dry_run=dry_run)
        if ok:
            success += 1

    print(json.dumps({"ticker": ticker, "total": len(docs), "success": success, "failed": len(docs) - success}))


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM-process downloaded IR documents.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", type=str, help="Company ticker to process")
    group.add_argument("--all", action="store_true", help="Process all tickers with unprocessed documents")
    parser.add_argument("--quarter", type=str, help="Filter to a specific quarter (e.g. Q3)")
    parser.add_argument("--year", type=str, help="Filter to a specific year (e.g. 2025)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be processed without calling LLM")
    args = parser.parse_args()

    if args.all:
        unprocessed = index_manager.get_unprocessed_documents()
        tickers = sorted({d["ticker"] for d in unprocessed})
        if not tickers:
            print("No unprocessed documents found.")
            return
        for t in tickers:
            run_for_ticker(t, args.quarter, args.year, args.dry_run)
    else:
        run_for_ticker(args.ticker, args.quarter, args.year, args.dry_run)


if __name__ == "__main__":
    main()
