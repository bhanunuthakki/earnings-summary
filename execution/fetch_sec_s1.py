"""
execution/fetch_sec_s1.py
-------------------------
Fetch the most-recent S-1 / S-1/A / 424B prospectus narrative text from SEC
EDGAR and cache it at ``data/sec_text/<TICKER>_s1_<FY>.txt``.

Sibling to the 10-K text fetcher path used by the rest of the pipeline. The
S-1 is the canonical disclosure document for recently-IPO'd issuers (no
10-K filed yet); it carries the same business description, TAM, risk
factors, MD&A, and audited historical financials a 10-K would, so prompts
that anchor on 10-K narrative can fall back to the S-1 cache for tickers
flagged ``recently_ipod`` in their holdings JSON.

Usage:
    python execution/fetch_sec_s1.py --ticker FRVO
    python execution/fetch_sec_s1.py --ticker FRVO --user-agent "Name <you@example.com>"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from filing_text_fetcher import fetch_latest_s1_text  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ticker", required=True, help="Ticker symbol (e.g. FRVO)")
    ap.add_argument(
        "--user-agent",
        default=None,
        help='SEC requires a contact-string UA, e.g. "Name <email@example.com>"',
    )
    ap.add_argument(
        "--cache-dir",
        default=str(PROJECT_ROOT / "data" / "sec_text"),
        help="Directory to write cached S-1 text into",
    )
    args = ap.parse_args()

    cache_dir = Path(args.cache_dir)
    result = fetch_latest_s1_text(
        ticker=args.ticker.upper(),
        user_agent=args.user_agent,
        cache_dir=cache_dir,
    )
    if result is None:
        print(
            json.dumps(
                {"event": "s1_not_found", "ticker": args.ticker.upper()},
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1
    cache_file = cache_dir / f"{result.ticker}_s1_{result.fiscal_year}.txt"
    print(
        json.dumps(
            {
                "event": "s1_fetched",
                "ticker": result.ticker,
                "accession": result.accession_number,
                "filing_date": result.filing_date,
                "url": result.primary_doc_url,
                "chars": len(result.text),
                "cache_path": str(cache_file),
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
