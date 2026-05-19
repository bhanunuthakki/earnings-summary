"""execution/intake_documents.py
-------------------------------
Layer 3 CLI: scan `_inbox/` for user-dropped documents, classify each one, and
file it into the canonical layout.

Files filed by this command:
  PDF / TXT  →  ir_documents/<TICKER>/<period_end>/ir_<doctype>__<sha8>.<ext>
  Audio      →  transcripts/raw/<TICKER>_Q<n>_<YYYY>.<ext>

After filing, optionally chains into `process_ir_documents.py` for each
ticker that received new IR documents (`--process` flag).

Usage:
    python execution/intake_documents.py
    python execution/intake_documents.py --dry-run
    python execution/intake_documents.py --process
    python execution/intake_documents.py --inbox path/to/other/folder
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

# Make `src/` importable
SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from intake import INBOX_DIR, IntakeResult, scan_inbox  # noqa: E402
from models.documents import DocType  # noqa: E402


def summarize(results: list[IntakeResult]) -> dict:
    skip_reasons: Counter[str] = Counter(
        r.skip_reason for r in results if r.skipped and r.skip_reason
    )
    by_doc_type: Counter[str] = Counter(
        r.classification.doc_type.value
        for r in results
        if not r.skipped and r.classification is not None
    )
    return {
        "scanned": len(results),
        "filed": sum(1 for r in results if not r.skipped),
        "skipped": sum(1 for r in results if r.skipped),
        "by_skip_reason": dict(skip_reasons),
        "by_doc_type": dict(by_doc_type),
    }


def chain_processing(results: list[IntakeResult]) -> None:
    """Run downstream pipelines for each ticker that got new IR docs.

    Every ticker with new docs gets `process_ir_documents.py` (LLM summary).
    Tickers that received transcripts additionally get
    `ingest_transcripts.py --include-ir-transcripts` (bridges intake-filed
    transcripts into the `transcripts` + `transcript_segments` tables) and
    `extract_commitments_from_transcript.py --auto` (Say-Do extraction).

    This matches what `backfill_transcripts.py` runs for auto-fetched
    transcripts so the manual-drop path doesn't leave Say-Do blind.
    """
    new_filings = [
        r for r in results if not r.skipped and r.classification is not None
    ]
    if not new_filings:
        print("[intake] No newly-filed IR documents — skipping process chain.", file=sys.stderr)
        return

    tickers = sorted({r.classification.ticker for r in new_filings if r.classification})
    transcript_tickers = sorted({
        r.classification.ticker
        for r in new_filings
        if r.classification and r.classification.doc_type == DocType.EARNINGS_CALL_TRANSCRIPT
    })

    process_script = SCRIPT_DIR / "process_ir_documents.py"
    ingest_script = SCRIPT_DIR / "ingest_transcripts.py"
    commitments_script = SCRIPT_DIR / "extract_commitments_from_transcript.py"

    for ticker in tickers:
        print(f"[intake] Chaining: process_ir_documents.py --ticker {ticker}", file=sys.stderr)
        subprocess.run(
            [sys.executable, str(process_script), "--ticker", ticker],
            check=False,
        )

    for ticker in transcript_tickers:
        print(
            f"[intake] Chaining: ingest_transcripts.py --ticker {ticker} --include-ir-transcripts",
            file=sys.stderr,
        )
        subprocess.run(
            [
                sys.executable,
                str(ingest_script),
                "--ticker",
                ticker,
                "--include-ir-transcripts",
            ],
            check=False,
        )
        print(
            f"[intake] Chaining: extract_commitments_from_transcript.py --auto --ticker {ticker}",
            file=sys.stderr,
        )
        subprocess.run(
            [
                sys.executable,
                str(commitments_script),
                "--auto",
                "--ticker",
                ticker,
            ],
            check=False,
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Classify user-dropped documents in _inbox/ and file them into the canonical "
            "ir_documents/<TICKER>/<period_end>/ layout. PDFs and text transcripts route "
            "to ir_documents/; audio routes to transcripts/raw/ for the whisper pipeline."
        ),
    )
    parser.add_argument(
        "--inbox", type=Path, default=INBOX_DIR,
        help=f"Drop folder to scan (default: {INBOX_DIR.relative_to(PROJECT_ROOT)})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Plan classifications and moves without applying them",
    )
    parser.add_argument(
        "--process", action="store_true",
        help="After filing, run process_ir_documents.py for each affected ticker",
    )
    args = parser.parse_args()

    results = scan_inbox(args.inbox, dry_run=args.dry_run)
    summary = summarize(results)
    print(json.dumps(summary, indent=2))

    if args.process and not args.dry_run:
        chain_processing(results)


if __name__ == "__main__":
    main()
