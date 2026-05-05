"""
execution/register_dropped_documents.py
---------------------------------------
Layer 3 CLI: Scans micro_thesis/sources/<TICKER>/ for dropped PDFs and registers
them in document_index.json so process_ir_documents.py can pick them up.

Closes the gap where micro_thesis_skill.md promises drop-and-process behavior
but no execution path actually consumed micro_thesis/sources/.

Idempotent: re-running with no new files (or no changed local_path for already-
registered docs) is a no-op.

Usage:
    python execution/register_dropped_documents.py --ticker RBRK
    python execution/register_dropped_documents.py --all
    python execution/register_dropped_documents.py --dry-run --all
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import TypedDict

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

import index_manager
from alias_manager import resolve_ticker

SOURCES_DIR = PROJECT_ROOT / "micro_thesis" / "sources"

LOG_FORMAT = json.dumps({"level": "%(levelname)s", "ts": "%(asctime)s", "msg": "%(message)s"})
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stderr)
log = logging.getLogger(__name__)


class FileSpec(TypedDict):
    quarter: str
    year: str
    doc_type: str
    note: str


# Per-file overrides for dropped PDFs whose filenames don't follow a clean
# parseable convention. Quarter/year reflect the FISCAL period the doc covers.
# doc_type ∈ {transcript, press_release, presentation}.
#
# Add an entry here when onboarding a new dropped file. If a file in
# micro_thesis/sources/<TICKER>/ has no override entry, the script logs a warning
# and skips it (no fuzzy filename guessing — keeps the no-hallucination contract).
OVERRIDES: dict[str, dict[str, FileSpec]] = {
    "BN": {
        "BN_2025-Investor-Day-Presentation.pdf": {
            "quarter": "Q3", "year": "2025", "doc_type": "presentation",
            "note": "Investor Day deck (Sept 2025) — not a quarterly earnings deck",
        },
        "BN_2025-AGM-Presentation.pdf": {
            "quarter": "Q2", "year": "2025", "doc_type": "presentation",
            "note": "AGM deck (May 2025) — not a quarterly earnings deck",
        },
    },
    "MELI": {
        "MELI_Q1-2025-Earnings-Presentation.pdf": {"quarter": "Q1", "year": "2025", "doc_type": "presentation", "note": ""},
        "MELI_Q2-2025-Earnings-Presentation.pdf": {"quarter": "Q2", "year": "2025", "doc_type": "presentation", "note": ""},
        "MELI_Q3-2025-Earnings-Presentation.pdf": {"quarter": "Q3", "year": "2025", "doc_type": "presentation", "note": ""},
        "MELI_Q4-2025-Earnings-Presentation.pdf": {"quarter": "Q4", "year": "2025", "doc_type": "presentation", "note": ""},
        "MELI_10-K-Annual-Report-FY2025.pdf": {
            "quarter": "Q4", "year": "2025", "doc_type": "press_release",
            "note": "10-K FY2025 — covers Q4'25 results and full year",
        },
    },
    "NOW": {
        "NOW_Q4-2025-Earnings-Results.pdf": {"quarter": "Q4", "year": "2025", "doc_type": "press_release", "note": ""},
        "NOW_Q4-2025-Investor-Presentation.pdf": {"quarter": "Q4", "year": "2025", "doc_type": "presentation", "note": ""},
        "NOW-ER-Q1-FY26.pdf": {
            "quarter": "Q1", "year": "2026", "doc_type": "press_release",
            "note": "Q1 FY26 earnings release (NOW fiscal calendar = calendar)",
        },
        "ServiceNow-1Q26-Investor-Presentation.pdf": {"quarter": "Q1", "year": "2026", "doc_type": "presentation", "note": ""},
    },
    "NU": {
        "NU_1Q25-Earnings-Presentation.pdf": {"quarter": "Q1", "year": "2025", "doc_type": "presentation", "note": ""},
        "NU_2Q25-Earnings-Presentation.pdf": {"quarter": "Q2", "year": "2025", "doc_type": "presentation", "note": ""},
        "NU_3Q25-Earnings-Presentation.pdf": {"quarter": "Q3", "year": "2025", "doc_type": "presentation", "note": ""},
        "NU_4Q25-Earnings-Presentation.pdf": {"quarter": "Q4", "year": "2025", "doc_type": "presentation", "note": ""},
    },
    "NVO": {
        "NVO_Q1-2025-Investor-Presentation.pdf": {"quarter": "Q1", "year": "2025", "doc_type": "presentation", "note": ""},
        "NVO_Q2-2025-Investor-Presentation.pdf": {"quarter": "Q2", "year": "2025", "doc_type": "presentation", "note": ""},
        "NVO_Q3-2025-Investor-Presentation.pdf": {"quarter": "Q3", "year": "2025", "doc_type": "presentation", "note": ""},
        "NVO_FY2025-Q4-Investor-Presentation.pdf": {"quarter": "Q4", "year": "2025", "doc_type": "presentation", "note": ""},
        "Novo 25 Annual Report.pdf": {
            "quarter": "Q4", "year": "2025", "doc_type": "press_release",
            "note": "FY2025 Annual Report — covers Q4'25 results and full year",
        },
    },
    "RBRK": {
        # Note: pipeline doc_types are {transcript, press_release, presentation}; there is no
        # SEC filing type. Of the two RBRK Q4 'press_release' candidates, the standalone PR
        # is preferred over the 10-Q because it carries management commentary + KPIs the LLM
        # summarizer was tuned for. The 10-Q remains in micro_thesis/sources/RBRK/ for manual
        # reference but is excluded from the index until a proper 'filing' doc_type lands.
        "RBRK_Q4-FY2026-Press-Release.pdf": {
            "quarter": "Q4", "year": "2026", "doc_type": "press_release",
            "note": "Q4 FY2026 (RBRK FY ends Jan; Q4 FY26 ≈ Feb–Apr 2026)",
        },
        "RBRK_Q4-FY2026-Earnings-Presentation.pdf": {"quarter": "Q4", "year": "2026", "doc_type": "presentation", "note": ""},
    },
    "VEEV": {
        "VEEV_Q4-FY2026-Earnings-Release.pdf": {
            "quarter": "Q4", "year": "2026", "doc_type": "press_release",
            "note": "Q4 FY2026 (VEEV FY ends Jan; Q4 FY26 ≈ Nov 2025–Jan 2026)",
        },
        "VEEV_Q4-FY2026-Earnings-Presentation.pdf": {"quarter": "Q4", "year": "2026", "doc_type": "presentation", "note": ""},
        "VEEV_Q4-FY2026-Prepared-Remarks.pdf": {
            "quarter": "Q4", "year": "2026", "doc_type": "transcript",
            "note": "Prepared remarks (proxy for transcript — Q&A absent)",
        },
    },
}


def dedupe_by_size(pdfs: list[Path], overrides: dict[str, FileSpec]) -> list[Path]:
    """Keep one PDF per byte size; prefer the override-named copy when duplicates exist."""
    by_size: dict[int, list[Path]] = {}
    for p in pdfs:
        by_size.setdefault(p.stat().st_size, []).append(p)
    keep: list[Path] = []
    for paths in by_size.values():
        with_override = [p for p in paths if p.name in overrides]
        chosen = sorted(with_override)[0] if with_override else sorted(paths)[0]
        keep.append(chosen)
    return sorted(keep)


def detect_key_collisions(ticker: str, deduped: list[Path], overrides: dict[str, FileSpec]) -> list[tuple[str, list[str]]]:
    """
    Find any (year, quarter, doc_type) keys that would be assigned by ≥2 files.
    The document_index is keyed on this tuple, so collisions silently overwrite.
    Returns a list of (key, [colliding_filenames]).
    """
    key_to_files: dict[str, list[str]] = {}
    for pdf in deduped:
        spec = overrides.get(pdf.name)
        if spec is None:
            continue
        key = f"{ticker}_{spec['year']}_{spec['quarter']}_{spec['doc_type']}"
        key_to_files.setdefault(key, []).append(pdf.name)
    return [(k, v) for k, v in key_to_files.items() if len(v) > 1]


def register_for_ticker(ticker: str, dry_run: bool = False) -> dict:
    ticker = resolve_ticker(ticker).upper()
    ticker_dir = SOURCES_DIR / ticker
    if not ticker_dir.exists():
        log.warning({"event": "no_source_dir", "ticker": ticker, "path": str(ticker_dir)})
        return {"ticker": ticker, "registered": 0, "skipped_existing": 0, "no_override": [], "collisions": []}

    overrides = OVERRIDES.get(ticker, {})
    if not overrides:
        log.warning({"event": "no_overrides_for_ticker", "ticker": ticker})

    pdfs = list(ticker_dir.glob("*.pdf"))
    deduped = dedupe_by_size(pdfs, overrides)

    # Fail loudly on collisions: doc_index is keyed on (ticker, year, quarter, doc_type),
    # so two files mapped to the same key would silently overwrite each other.
    collisions = detect_key_collisions(ticker, deduped, overrides)
    if collisions:
        for key, files in collisions:
            log.error({"event": "key_collision", "ticker": ticker, "key": key, "files": files})
        raise ValueError(
            f"[{ticker}] {len(collisions)} key collision(s) in OVERRIDES — multiple files mapped to "
            f"the same (year, quarter, doc_type). Fix by giving each file a distinct doc_type, OR "
            f"by removing the duplicate from OVERRIDES. Collisions: {collisions}"
        )

    registered = 0
    skipped_existing = 0
    no_override: list[str] = []

    for pdf in deduped:
        spec = overrides.get(pdf.name)
        if spec is None:
            no_override.append(pdf.name)
            log.warning({"event": "no_override", "ticker": ticker, "file": pdf.name})
            continue

        existing = index_manager.has_document(ticker, spec["year"], spec["quarter"], spec["doc_type"])
        if existing and existing.get("local_path") == str(pdf):
            skipped_existing += 1
            log.info({
                "event": "already_registered",
                "ticker": ticker,
                "key": f"{ticker}_{spec['year']}_{spec['quarter']}_{spec['doc_type']}",
            })
            continue

        if dry_run:
            log.info({
                "event": "would_register",
                "ticker": ticker,
                "file": pdf.name,
                "key": f"{ticker}_{spec['year']}_{spec['quarter']}_{spec['doc_type']}",
            })
            registered += 1
            continue

        index_manager.register_manual_document(
            ticker=ticker,
            year=spec["year"],
            quarter=spec["quarter"],
            doc_type=spec["doc_type"],
            local_path=str(pdf),
            note=spec["note"] or None,
            processed=False,
        )
        log.info({
            "event": "registered",
            "ticker": ticker,
            "file": pdf.name,
            "key": f"{ticker}_{spec['year']}_{spec['quarter']}_{spec['doc_type']}",
        })
        registered += 1

    return {
        "ticker": ticker,
        "registered": registered,
        "skipped_existing": skipped_existing,
        "no_override": no_override,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Register dropped PDFs from micro_thesis/sources/<TICKER>/ into document_index.json."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", type=str, help="Ticker to register (e.g. RBRK)")
    group.add_argument("--all", action="store_true", help="Register all tickers with override entries")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be registered without writing the index")
    args = parser.parse_args()

    tickers = sorted(OVERRIDES.keys()) if args.all else [args.ticker.upper()]

    summaries = []
    for t in tickers:
        summaries.append(register_for_ticker(t, dry_run=args.dry_run))

    print(json.dumps({"dry_run": args.dry_run, "results": summaries}, indent=2))


if __name__ == "__main__":
    main()
