"""Track Infineon, Procore, and Toast into the evaluation list."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

from onboard_ticker import apply_industry_template  # noqa: E402

import db  # noqa: E402
from entity_store import upsert_entity  # noqa: E402


def main() -> int:
    tickers = [
        ("IFNNY", "Infineon Technologies AG", "evaluation", "adr", "20-F"),
        ("PCOR", "PROCORE TECHNOLOGIES, INC.", "evaluation", "equity", "10-K"),
        ("TOST", "Toast, Inc.", "evaluation", "equity", "10-K"),
    ]

    for ticker, name, list_type, inst_type, filing_regime in tickers:
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
        sector = "Semiconductors" if ticker == "IFNNY" else "Software / Technology"
        db_path = PROJECT_ROOT / "data" / "portfolio.db"
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
