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


def quarter_to_period_end(year: int | str, quarter: str) -> str:
    """
    Map (year, quarter) → ISO period-end date using calendar-quarter heuristic.
    Q1→Mar 31, Q2→Jun 30, Q3→Sep 30, Q4→Dec 31. Issuers with off-calendar fiscal
    years (VEEV, RBRK ending Jan; etc.) will be approximated; this is acceptable
    for staleness detection where ±60 days of slop does not materially change
    the >120-day stale threshold.
    """
    q = quarter.upper().lstrip("Q")
    quarter_int = int(q)
    if quarter_int not in (1, 2, 3, 4):
        raise ValueError(f"Invalid quarter: {quarter}")
    end_map = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}
    return f"{int(year)}-{end_map[quarter_int]}"


def latest_corpus_date(quarters: list[dict]) -> str | None:
    """Return ISO date of the most recent period_end across quarters in scope, or None if empty."""
    if not quarters:
        return None
    return max(quarter_to_period_end(q["year"], q["quarter"]) for q in quarters)


def _cache_name(ticker: str, quarter: str, year, doc_type: str) -> str:
    suffix_map = {
        "transcript": "summary.txt",
        "press_release": "press_release_summary.txt",
        "presentation": "presentation_brief.txt",
    }
    suffix = suffix_map.get(doc_type, f"{doc_type}.txt")
    return f"{ticker}_{quarter}_{year}_{suffix}"


def update_tracker(ticker: str) -> str:
    """
    Generate and write a tracker for one ticker.

    Returns one of: "done", "skipped_no_schema", "skipped_no_docs", "failed".
    Never raises — failures are logged and returned as status so batch runs
    in --all mode can continue past a single-ticker failure.
    """
    ticker = resolve_ticker(ticker).upper()
    schema = load_holdings_schema(ticker)
    if schema is None:
        print(f"[{ticker}] No holdings schema found. Skipping.", file=sys.stderr)
        return "skipped_no_schema"

    quarters = gather_quarter_context(ticker)
    if not quarters:
        print(f"[{ticker}] No processed documents found. Run process_ir_documents.py first.", file=sys.stderr)
        return "skipped_no_docs"

    report_date = datetime.date.today().isoformat()
    corpus_latest = latest_corpus_date(quarters)

    log.info({
        "event": "generating_tracker",
        "ticker": ticker,
        "quarters_available": len(quarters),
        "report_date": report_date,
        "corpus_latest_date": corpus_latest,
    })

    try:
        tracker_text = generate_thesis_update(ticker, schema, quarters, report_date, corpus_latest)
    except Exception as e:
        log.error({"event": "llm_failed", "ticker": ticker, "error": str(e)})
        print(json.dumps({"ticker": ticker, "status": "failed", "error": str(e)}))
        return "failed"

    out_path = THESIS_DIR / f"thesis-tracker-{ticker}-{report_date}.md"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(tracker_text)

    print(json.dumps({"ticker": ticker, "status": "done", "output": str(out_path)}))
    log.info({"event": "tracker_written", "path": str(out_path)})
    return "done"


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
        results: dict[str, list[str]] = {"done": [], "skipped_no_docs": [], "skipped_no_schema": [], "failed": []}
        for schema_path in schemas:
            ticker = schema_path.stem
            status = update_tracker(ticker)
            results[status].append(ticker)
        # Final batch summary on stderr — visible in the run log without polluting JSON-line stdout.
        print(json.dumps({"event": "batch_summary", **{k: v for k, v in results.items() if v}}), file=sys.stderr)
        # Non-zero exit if any ticker failed; skips are not failures.
        if results["failed"]:
            sys.exit(1)
    else:
        status = update_tracker(args.ticker)
        if status == "failed":
            sys.exit(1)


if __name__ == "__main__":
    main()
