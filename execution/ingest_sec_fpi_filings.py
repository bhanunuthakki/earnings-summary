"""CLI entrypoint to ingest Form 6-K (quarterly) and Form 20-F (annual) filings for FPIs.

Usage:
    python execution/ingest_sec_fpi_filings.py --ticker WIX --year 2026 --quarter Q2
    python execution/ingest_sec_fpi_filings.py --all-fpi --year 2026 --quarter Q2
    python execution/ingest_sec_fpi_filings.py --ticker NU --year 2025 --form 20-F
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pipeline.queries import open_db  # noqa: E402
from pipeline.sec_fpi_ingest import FpiIngestResult, ingest_fpi_for_ticker  # noqa: E402
from table_extractors.period_axis import NominalQuarter  # noqa: E402

log = logging.getLogger(__name__)


def list_tracked_fpis(conn: sqlite3.Connection) -> list[str]:
    """Find all active tracked companies with 20-F / 40-F filing regime or ADR instrument type."""
    rows = conn.execute(
        """
        SELECT ticker FROM tracked_companies
        WHERE archived_at IS NULL
          AND list_type IN ('portfolio', 'evaluation', 'watchlist')
          AND (filing_regime IN ('20-F', '40-F') OR instrument_type = 'adr')
        ORDER BY ticker ASC
        """
    ).fetchall()
    return [r[0].upper() for r in rows]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ingest Foreign Private Issuer (FPI) 6-K and 20-F filings from SEC EDGAR."
    )
    parser.add_argument("--ticker", type=str, help="Target ticker (e.g. WIX, NU, NVO)")
    parser.add_argument(
        "--all-fpi", action="store_true", help="Ingest all active tracked FPI companies"
    )
    parser.add_argument(
        "--year",
        type=int,
        default=datetime.now().year,
        help="Target fiscal year (default: current)",
    )
    parser.add_argument(
        "--quarter",
        type=str,
        choices=["Q1", "Q2", "Q3", "Q4"],
        default=None,
        help="Target nominal quarter",
    )
    parser.add_argument(
        "--form",
        type=str,
        choices=["6-K", "20-F"],
        default="6-K",
        help="SEC filing form type (default: 6-K)",
    )
    parser.add_argument(
        "--force", action="store_true", help="Force re-extraction and overwrite existing facts"
    )

    args = parser.parse_args()

    if not args.ticker and not args.all_fpi:
        parser.error("Must specify either --ticker <TICKER> or --all-fpi")

    db_path = PROJECT_ROOT / "data" / "portfolio.db"
    conn = open_db(db_path)

    tickers: list[str] = []
    if args.ticker:
        tickers = [args.ticker.upper()]
    elif args.all_fpi:
        tickers = list_tracked_fpis(conn)

    if not tickers:
        sys.stderr.write(
            json.dumps({"event": "warning", "message": "No matching FPI tickers found"}) + "\n"
        )
        print(json.dumps({"status": "ok", "ingested": 0, "results": []}))
        return 0

    results: list[dict[str, object]] = []
    total_facts = 0
    total_kpis = 0

    for ticker in tickers:
        sys.stderr.write(
            json.dumps(
                {
                    "event": "ingesting_fpi",
                    "ticker": ticker,
                    "year": args.year,
                    "quarter": args.quarter,
                    "form": args.form,
                }
            )
            + "\n"
        )

        nom_quarter = cast("NominalQuarter | None", args.quarter)
        res: FpiIngestResult = ingest_fpi_for_ticker(
            conn,
            ticker=ticker,
            year=args.year,
            quarter=nom_quarter,
            form=args.form,
            repo_root=PROJECT_ROOT,
            force=args.force,
        )

        results.append(
            {
                "ticker": res.ticker,
                "form_type": res.form_type,
                "accession": res.accession,
                "filing_date": res.filing_date,
                "document_id": res.document_id,
                "facts_inserted": res.facts_inserted,
                "kpis_inserted": res.kpis_inserted,
                "status": res.status,
                "error_message": res.error_message,
            }
        )

        if res.status == "ok":
            total_facts += res.facts_inserted
            total_kpis += res.kpis_inserted
            conn.commit()

    summary = {
        "status": "ok",
        "processed_count": len(tickers),
        "total_financial_facts": total_facts,
        "total_kpi_facts": total_kpis,
        "results": results,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
