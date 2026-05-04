"""
execution/update_thesis_tracker.py
------------------------------------
Layer 3 CLI: Refresh the micro-thesis tracker for a holding using all available
cached summaries (transcript, press release, presentation briefs).

Reads:
  - micro_thesis/holdings/<TICKER>.json  (KPI schema + thesis statement)
  - .tmp/<TICKER>_<Q>_<Y>_summary.txt
  - .tmp/<TICKER>_<Q>_<Y>_press_release_summary.txt
  - .tmp/<TICKER>_<Q>_<Y>_presentation_brief.txt

Writes:
  - micro_thesis/thesis-tracker-<TICKER>-<DATE>.md

Usage:
    python execution/update_thesis_tracker.py --ticker GOOG
    python execution/update_thesis_tracker.py --all
"""

import os
import sys
import json
import argparse
import datetime
import logging
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from alias_manager import resolve_ticker
from llm_client import generate_thesis_update
import index_manager

CACHE_DIR = PROJECT_ROOT / ".tmp"
THESIS_DIR = PROJECT_ROOT / "micro_thesis"
HOLDINGS_DIR = THESIS_DIR / "holdings"

LOG_FORMAT = json.dumps({"level": "%(levelname)s", "ts": "%(asctime)s", "msg": "%(message)s"})
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stderr)
log = logging.getLogger(__name__)


def load_holdings_schema(ticker: str) -> dict | None:
    path = HOLDINGS_DIR / f"{ticker.upper()}.json"
    if not path.exists():
        log.warning({"event": "no_holdings_schema", "ticker": ticker, "path": str(path)})
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def gather_quarter_context(ticker: str) -> list[dict]:
    """
    Collect all available cached summaries for a ticker, sorted chronologically.
    Each entry is a dict: {year, quarter, summaries: {doc_type: text}}.
    """
    ticker = ticker.upper()
    docs = index_manager.get_documents_for_ticker(ticker)

    # Group by (year, quarter)
    quarters: dict[tuple, dict] = {}
    for doc in docs:
        key = (str(doc["year"]), doc["quarter"].upper())
        if key not in quarters:
            quarters[key] = {"year": doc["year"], "quarter": doc["quarter"], "summaries": {}}

        doc_type = doc["doc_type"]
        cache_file = CACHE_DIR / _cache_name(ticker, doc["quarter"], doc["year"], doc_type)
        if cache_file.exists():
            with open(cache_file, "r", encoding="utf-8") as f:
                quarters[key]["summaries"][doc_type] = f.read()

    # Sort chronologically
    sorted_quarters = sorted(quarters.values(), key=lambda x: (str(x["year"]), x["quarter"]))
    return [q for q in sorted_quarters if q["summaries"]]  # Only quarters with at least one summary


def _cache_name(ticker: str, quarter: str, year, doc_type: str) -> str:
    suffix_map = {
        "transcript": "summary.txt",
        "press_release": "press_release_summary.txt",
        "presentation": "presentation_brief.txt",
    }
    suffix = suffix_map.get(doc_type, f"{doc_type}.txt")
    return f"{ticker}_{quarter}_{year}_{suffix}"


def update_tracker(ticker: str) -> None:
    ticker = resolve_ticker(ticker).upper()
    schema = load_holdings_schema(ticker)
    if schema is None:
        print(f"[{ticker}] No holdings schema found. Skipping.", file=sys.stderr)
        return

    quarters = gather_quarter_context(ticker)
    if not quarters:
        print(f"[{ticker}] No processed documents found. Run process_ir_documents.py first.", file=sys.stderr)
        return

    log.info({"event": "generating_tracker", "ticker": ticker, "quarters_available": len(quarters)})

    try:
        tracker_text = generate_thesis_update(ticker, schema, quarters)
    except Exception as e:
        log.error({"event": "llm_failed", "ticker": ticker, "error": str(e)})
        sys.exit(1)

    date_str = datetime.date.today().isoformat()
    out_path = THESIS_DIR / f"thesis-tracker-{ticker}-{date_str}.md"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(tracker_text)

    print(json.dumps({"ticker": ticker, "status": "done", "output": str(out_path)}))
    log.info({"event": "tracker_written", "path": str(out_path)})


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh micro-thesis tracker for a holding.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", type=str, help="Ticker to update (e.g. GOOG)")
    group.add_argument("--all", action="store_true", help="Update all tickers with holdings schemas")
    args = parser.parse_args()

    if args.all:
        schemas = list(HOLDINGS_DIR.glob("*.json"))
        if not schemas:
            print("No holdings schemas found.", file=sys.stderr)
            sys.exit(1)
        for schema_path in schemas:
            ticker = schema_path.stem
            update_tracker(ticker)
    else:
        update_tracker(args.ticker)


if __name__ == "__main__":
    main()
