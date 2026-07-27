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

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, cast

# Paths
SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

import index_manager  # noqa: E402
from alias_manager import resolve_ticker  # noqa: E402
from llm_client import (  # noqa: E402
    compose_anchor_block,
    generate_event_brief,
    generate_presentation_brief,
    generate_press_release_summary,
    generate_summary,
    load_bear_anchor,
    load_ir_anchor,
    load_thesis_anchor,
)
from parser import extract_text_from_pdf, read_text_file  # noqa: E402

CACHE_DIR = PROJECT_ROOT / ".tmp"

LOG_FORMAT = json.dumps({"level": "%(levelname)s", "ts": "%(asctime)s", "msg": "%(message)s"})
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stderr)
log = logging.getLogger(__name__)

# Seconds to sleep between LLM calls to avoid rate limiting
RATE_LIMIT_SLEEP = 15

# `uses_anchor` marks doc types whose LLM prompt accepts a thesis/bear-case
# anchor block (transcripts → analytical takeaway needs KPI anchor; events →
# §7 thesis-read-through needs the same). Press-release and presentation
# briefs are pure structured-data summaries — no anchor needed; their job is
# to be factual condensers, not to render thesis judgments.
DOC_TYPE_CONFIG: dict[str, dict] = {
    "press_release": {
        "cache_suffix": "press_release_summary.txt",
        "llm_fn": generate_press_release_summary,
        "label": "Press Release Summary",
        "uses_anchor": False,
    },
    "presentation": {
        "cache_suffix": "presentation_brief.txt",
        "llm_fn": generate_presentation_brief,
        "label": "Presentation Brief",
        "uses_anchor": False,
    },
    "transcript": {
        "cache_suffix": "summary.txt",
        "llm_fn": generate_summary,
        "label": "Transcript Summary",
        "uses_anchor": True,
    },
    # Letters to shareholders / investor updates summarize quarter highlights, so they
    # share the press-release LLM path. The cache suffix differs to keep artifacts distinct.
    "investor_update": {
        "cache_suffix": "investor_update_summary.txt",
        "llm_fn": generate_press_release_summary,
        "label": "Investor Update Summary",
        "uses_anchor": False,
    },
    # Non-quarterly IR events: investor days, AGMs, capital markets days, conference decks.
    # Uses the dedicated event-brief prompt focused on multi-year strategy + capital allocation.
    "event": {
        "cache_suffix": "event_brief.txt",
        "llm_fn": generate_event_brief,
        "label": "Event Brief",
        "uses_anchor": True,
    },
}


def _is_event(doc: dict) -> bool:
    return doc.get("doc_type") == "event"


def cache_path_for(doc: dict) -> Path:
    ticker = doc["ticker"]
    suffix = DOC_TYPE_CONFIG[doc["doc_type"]]["cache_suffix"]
    if _is_event(doc):
        # Event index entries have `event_date` instead of (year, quarter); the registered
        # local_path's filename ends in `__<sha8>.pdf` which we surface in the cache name.
        event_date = doc["event_date"]
        sha8 = Path(doc["local_path"]).stem.split("__")[-1]
        return CACHE_DIR / f"{ticker}_event_{event_date}_{sha8}_{suffix}"
    quarter = doc["quarter"]
    year = doc["year"]
    return CACHE_DIR / f"{ticker}_{quarter}_{year}_{suffix}"


def _mark_processed(doc: dict) -> None:
    if _is_event(doc):
        sha8 = Path(doc["local_path"]).stem.split("__")[-1]
        index_manager.mark_event_processed(doc["ticker"], doc["event_date"], sha8)
        return
    index_manager.mark_document_processed(
        doc["ticker"], doc["year"], doc["quarter"], doc["doc_type"]
    )


def _doc_log_fields(doc: dict) -> dict:
    if _is_event(doc):
        return {"ticker": doc["ticker"], "event_date": doc["event_date"], "doc_type": "event"}
    return {
        "ticker": doc["ticker"],
        "quarter": doc["quarter"],
        "year": doc["year"],
        "doc_type": doc["doc_type"],
    }


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
    doc_type = doc["doc_type"]
    local_path = doc.get("local_path")
    log_fields = _doc_log_fields(doc)

    config = DOC_TYPE_CONFIG.get(doc_type)
    if config is None:
        log.warning({"event": "unknown_doc_type", **log_fields})
        return False

    if not local_path or not Path(local_path).exists():
        log.warning({"event": "local_path_missing", **log_fields})
        return False

    cache = cache_path_for(doc)

    if cache.exists():
        log.info({"event": "cache_hit", "cache": str(cache)})
        _mark_processed(doc)
        return True

    log.info({"event": "processing", "label": config["label"], **log_fields})

    if dry_run:
        print(f"[DRY RUN] Would process: {log_fields} -> {cache.name}")
        return True

    try:
        text = extract_text(local_path)
        if not text.strip():
            log.error({"event": "empty_text", "local_path": local_path})
            return False

        if config.get("uses_anchor", False):
            anchor_block = compose_anchor_block(
                load_thesis_anchor(PROJECT_ROOT, doc["ticker"]),
                load_bear_anchor(PROJECT_ROOT, doc["ticker"]),
                load_ir_anchor(PROJECT_ROOT, doc["ticker"]),
            )
            result_text = config["llm_fn"](text, anchor_block=anchor_block)
        else:
            result_text = config["llm_fn"](text)

        with open(cache, "w", encoding="utf-8") as f:
            f.write(result_text)

        log.info({"event": "cached", "cache": str(cache)})
        _mark_processed(doc)
        time.sleep(RATE_LIMIT_SLEEP)
        return True

    except Exception as e:
        log.error({"event": "processing_failed", "error": str(e), **log_fields})
        return False


def run_for_ticker(
    ticker: str,
    quarter: str | None = None,
    year: str | None = None,
    dry_run: bool = False,
    regenerate_missing: bool = False,
) -> None:
    ticker = resolve_ticker(ticker).upper()
    if regenerate_missing:
        # Self-heal: re-include every registered quarterly doc regardless of its
        # `processed` flag. A transcript registered processed=True at ingest
        # (index_manager.register_transcript) whose `_summary.txt` was never
        # written is otherwise skipped forever by get_unprocessed_documents —
        # the gap that leaves §6 Say-Do empty for freshly-onboarded names.
        # process_document early-returns on cache-hit, so docs that already have
        # a summary are skipped (no re-billing); only missing artifacts regenerate.
        docs = [
            d
            for d in cast(
                list[dict[str, Any]],
                index_manager.get_documents_for_ticker(ticker),
            )
            if d.get("doc_type") in DOC_TYPE_CONFIG
            and d.get("doc_type") != "event"
            and d.get("local_path")
        ]
    else:
        docs = index_manager.get_unprocessed_documents(ticker)

    if quarter:
        docs = [d for d in docs if d["quarter"].upper() == quarter.upper()]
    if year:
        docs = [d for d in docs if str(d["year"]) == str(year)]

    # Events: include unless the caller filtered by quarter (events have no quarter).
    # `--year` filters events by event_date year so users can scope to a specific period.
    # regenerate_missing intentionally scopes to quarterly docs (the Say-Do inputs).
    if not quarter and not regenerate_missing:
        events = index_manager.get_unprocessed_events(ticker)
        if year:
            events = [e for e in events if e.get("event_date", "").startswith(str(year))]
        docs = docs + events

    if not docs:
        print(f"[{ticker}] No unprocessed documents found.")
        return

    print(f"[{ticker}] Processing {len(docs)} document(s)...")
    success = 0
    for doc in docs:
        ok = process_document(doc, dry_run=dry_run)
        if ok:
            success += 1

    print(
        json.dumps(
            {
                "ticker": ticker,
                "total": len(docs),
                "success": success,
                "failed": len(docs) - success,
            }
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM-process downloaded IR documents.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", type=str, help="Company ticker to process")
    group.add_argument(
        "--all", action="store_true", help="Process all tickers with unprocessed documents"
    )
    parser.add_argument("--quarter", type=str, help="Filter to a specific quarter (e.g. Q3)")
    parser.add_argument("--year", type=str, help="Filter to a specific year (e.g. 2025)")
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be processed without calling LLM"
    )
    parser.add_argument(
        "--regenerate-missing",
        action="store_true",
        help="Re-include every registered quarterly doc whose summary cache file "
        "is absent, even if marked processed. Self-heals transcripts registered "
        "processed=True without a summary ever being written (the gap that leaves "
        "§6 Say-Do empty). Never re-bills a doc that already has a cached summary. "
        "Intended with --ticker.",
    )
    args = parser.parse_args()

    if args.all:
        unprocessed = (
            index_manager.get_unprocessed_documents() + index_manager.get_unprocessed_events()
        )
        tickers = sorted({d["ticker"] for d in unprocessed})
        if not tickers:
            print("No unprocessed documents found.")
            return
        for t in tickers:
            run_for_ticker(t, args.quarter, args.year, args.dry_run, args.regenerate_missing)
    else:
        run_for_ticker(args.ticker, args.quarter, args.year, args.dry_run, args.regenerate_missing)


if __name__ == "__main__":
    main()
