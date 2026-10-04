"""Track Infineon, Procore, and Toast into the evaluation list."""

from __future__ import annotations

import sys
from pathlib import Path

from onboard_ticker import apply_industry_template

import db
from entity_store import upsert_entity

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    tickers = [
        ("IFNNY", "Infineon Technologies AG", "evaluation", "adr", "20-F", "Semiconductors"),
        (
            "PCOR",
            "PROCORE TECHNOLOGIES, INC.",
            "evaluation",
            "equity",
            "10-K",
            "Software / Technology",
        ),
        ("TOST", "Toast, Inc.", "evaluation", "equity", "10-K", "Software / Technology"),
        (
            "TSM",
            "Taiwan Semiconductor Manufacturing Co Ltd",
            "evaluation",
            "adr",
            "20-F",
            "Semiconductors",
        ),
        ("LITE", "Lumentum Holdings Inc.", "evaluation", "equity", "10-K", "Technology"),
        ("CPNG", "Coupang, Inc.", "evaluation", "equity", "10-K", "Consumer Discretionary"),
        (
            "ONON",
            "On Holding AG",
            "evaluation",
            "foreign_private_issuer",
            "20-F",
            "Consumer Discretionary",
        ),
    ]

    for ticker, name, list_type, inst_type, filing_regime, sector in tickers:
        print(f"Tracking {ticker} ({name}) as {list_type}...")
        db.track_company(ticker, name, list_type)

        conn = db.get_connection()
        try:
            conn.execute(
                "UPDATE tracked_companies SET instrument_type = ?, filing_regime = ? WHERE ticker = ?",
                (inst_type, filing_regime, ticker),
            )
            conn.commit()
        finally:
            conn.close()

        # Apply industry template if applicable
        if ticker in ("PCOR", "TOST"):
            apply_industry_template(
                ticker=ticker,
                industry_slug="software_saas",
                repo_root=PROJECT_ROOT,
            )

        # Seed entity
        db_path = Path(db.DB_PATH)
        upsert_entity(
            kind="company",
            canonical_name=name,
            display_name=name,
            external_ids={"ticker": ticker},
            meta={"sector": sector},
            db_path=db_path,
        )
        print(f"Tracked and configured {ticker}.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
