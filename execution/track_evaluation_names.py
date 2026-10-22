"""Track Infineon, Procore, and Toast into the evaluation list."""

from __future__ import annotations

import sys
from pathlib import Path

from onboard_ticker import apply_industry_template

import db
from entity_store import upsert_entity
from models.companies import FilingRegime, InstrumentType

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_EQUITY = InstrumentType.EQUITY
_ADR = InstrumentType.ADR
_10K = FilingRegime.FORM_10K
_20F = FilingRegime.FORM_20F

# instrument_type names the listed security; filing_regime names the SEC forms.
# A foreign private issuer that lists ordinary shares (ONON) is equity + 20-F.
EVALUATION_NAMES: tuple[tuple[str, str, str, InstrumentType, FilingRegime, str], ...] = (
    ("IFNNY", "Infineon Technologies AG", "evaluation", _ADR, _20F, "Semiconductors"),
    ("PCOR", "PROCORE TECHNOLOGIES, INC.", "evaluation", _EQUITY, _10K, "Software / Technology"),
    ("TOST", "Toast, Inc.", "evaluation", _EQUITY, _10K, "Software / Technology"),
    (
        "TSM",
        "Taiwan Semiconductor Manufacturing Co Ltd",
        "evaluation",
        _ADR,
        _20F,
        "Semiconductors",
    ),
    ("LITE", "Lumentum Holdings Inc.", "evaluation", _EQUITY, _10K, "Technology"),
    ("CPNG", "Coupang, Inc.", "evaluation", _EQUITY, _10K, "Consumer Discretionary"),
    ("ONON", "On Holding AG", "evaluation", _EQUITY, _20F, "Consumer Discretionary"),
)


def main() -> int:
    for ticker, name, list_type, inst_type, filing_regime, sector in EVALUATION_NAMES:
        print(f"Tracking {ticker} ({name}) as {list_type}...")
        db.track_company(ticker, name, list_type)

        conn = db.get_connection()
        try:
            conn.execute(
                "UPDATE tracked_companies SET instrument_type = ?, filing_regime = ? WHERE ticker = ?",
                (inst_type.value, filing_regime.value, ticker),
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
