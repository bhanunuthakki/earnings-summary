"""Tests for src/table_extractors/lease_commitments.py.

Deterministic XBRL-section parser; no LLM calls, no mocking needed.
Fixtures use the exact shape FMP emits for the
'Leases - Future Minimum Lease Payments (Details)' section.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from table_extractors import lease_commitments as lc
from table_extractors.base import iter_xbrl_table, parse_units


def _schema(conn: sqlite3.Connection) -> None:
    """Inline lease_commitments + extractions schema for hermetic tests."""
    conn.executescript(
        """
        CREATE TABLE lease_commitments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR(16) NOT NULL,
            fiscal_year INTEGER NOT NULL,
            as_of_date DATE NOT NULL,
            filing_doc_id INTEGER,
            lease_type VARCHAR(16) NOT NULL,
            ladder_year VARCHAR(16) NOT NULL,
            ladder_calendar_year INTEGER,
            amount NUMERIC(24, 6) NOT NULL,
            currency VARCHAR(3) NOT NULL DEFAULT 'USD',
            unit VARCHAR(16) NOT NULL DEFAULT 'millions',
            source_section_key VARCHAR(255),
            extracted_at DATETIME NOT NULL,
            UNIQUE(ticker, fiscal_year, lease_type, ladder_year)
        );
        CREATE TABLE extractions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_doc_id INTEGER NOT NULL,
            char_offset_start INTEGER, char_offset_end INTEGER,
            extraction_kind VARCHAR(48) NOT NULL,
            extractor_id VARCHAR(64) NOT NULL,
            extractor_version VARCHAR(16) NOT NULL,
            payload_json TEXT NOT NULL,
            confidence FLOAT NOT NULL DEFAULT 1.0,
            extracted_at DATETIME NOT NULL,
            superseded_by_extraction_id INTEGER REFERENCES extractions(id),
            links_to_json TEXT
        );
        """
    )
    conn.commit()


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    p = tmp_path / "test.db"
    conn = sqlite3.connect(str(p))
    try:
        _schema(conn)
    finally:
        conn.close()
    return p


def _goog_lease_section() -> list[dict[str, object]]:
    """Exact shape of GOOG FY2024 'Leases - Future Minimum Lease Payments'.
    Per-period column order: [2024, 2023]."""
    return [
        {
            "Leases - Future Minimum Lease Payments (Details) - USD ($) $ in Millions":
                ["Dec. 31, 2024", "Dec. 31, 2023"]
        },
        {"Operating Leases": ["\xa0", "\xa0"]},
        {"2025": [3162, "\xa0"]},
        {"2026": [2824, "\xa0"]},
        {"2027": [2311, "\xa0"]},
        {"2028": [1838, "\xa0"]},
        {"2029": [1448, "\xa0"]},
        {"Thereafter": [5455, "\xa0"]},
        {"Total future lease payments": [17038, "\xa0"]},
        {"Less imputed interest": [-2460, "\xa0"]},
        {"Total lease liability balance": [14578, 15251]},
        {"Finance Leases": ["\xa0", "\xa0"]},
        {"2025": [257, "\xa0"]},
        {"2026": [208, "\xa0"]},
        {"2027": [208, "\xa0"]},
        {"2028": [197, "\xa0"]},
        {"2029": [166, "\xa0"]},
        {"Thereafter": [852, "\xa0"]},
        {"Total future lease payments": [1888, "\xa0"]},
        {"Less imputed interest": [-211, "\xa0"]},
        {"Total lease liability balance": [1677, 1666]},
    ]


# ---------------------------------------------------------------------------
# Unit-level tests on base helpers
# ---------------------------------------------------------------------------


def test_parse_units_usd_millions() -> None:
    u = parse_units("Leases - Future Minimum Lease Payments (Details) - USD ($) $ in Millions")
    assert u.currency == "USD"
    assert u.scale == "millions"


def test_parse_units_dkk_thousands() -> None:
    u = parse_units("Some Section - DKK (kr) kr in Thousands")
    assert u.currency == "DKK"
    assert u.scale == "thousands"


def test_parse_units_fallback() -> None:
    u = parse_units("Mystery section title with no scale")
    assert u.currency == "USD"  # fallback
    assert u.scale == "units"


def test_iter_xbrl_table_emits_axis_path_and_values() -> None:
    rows = list(iter_xbrl_table(_goog_lease_section()))
    # We expect 18 concrete rows (9 per lease_type: Y1..Y5, Thereafter,
    # Total, ImputedInterest, LeaseLiability — but the bare "Operating Leases"
    # and "Finance Leases" axis markers are NOT yielded).
    assert len(rows) == 18
    # First concrete row is 2025 under Operating Leases
    first = rows[0]
    assert first.label == "2025"
    assert first.axis_path == ["Operating Leases"]
    assert first.values == [3162, None]  # nbsp coerces to None
    # First period label decoded correctly
    assert first.period_labels == ["Dec. 31, 2024", "Dec. 31, 2023"]
    # Verify Finance Leases axis switches
    finance_rows = [r for r in rows if r.axis_path == ["Finance Leases"]]
    assert len(finance_rows) == 9


# ---------------------------------------------------------------------------
# Extractor-level tests
# ---------------------------------------------------------------------------


def test_extract_persists_all_ladder_rows(db: Path, tmp_path: Path) -> None:
    payload: dict[str, object] = {
        "symbol": "GOOG",
        "year": "2024",
        "Leases - Future Minimum Lease P": _goog_lease_section(),
    }
    outcome = lc.extract(
        ticker="GOOG",
        fiscal_year=2024,
        fmp_payload=payload,
        sec_text=None,
        db_path=db,
        repo_root=tmp_path,
        filing_doc_id=42,
        as_of_date=date(2024, 12, 31),
    )
    assert outcome.status == "ok"
    assert outcome.n_rows_proposed == 18  # 9 per lease_type
    assert outcome.n_rows_inserted == 18

    conn = sqlite3.connect(str(db))
    try:
        n = conn.execute("SELECT COUNT(*) FROM lease_commitments").fetchone()[0]
        assert n == 18
        # Spot-check: GOOG operating Y1 = 3162 with calendar year 2025
        row = conn.execute(
            "SELECT amount, ladder_calendar_year, currency, unit FROM lease_commitments "
            "WHERE ticker='GOOG' AND lease_type='operating' AND ladder_year='Y1'"
        ).fetchone()
        assert row == (3162, 2025, "USD", "millions")
        # Thereafter row has no calendar year
        row = conn.execute(
            "SELECT ladder_calendar_year FROM lease_commitments "
            "WHERE ticker='GOOG' AND lease_type='operating' AND ladder_year='Thereafter'"
        ).fetchone()
        assert row[0] is None
        # Imputed interest is negative
        row = conn.execute(
            "SELECT amount FROM lease_commitments "
            "WHERE ticker='GOOG' AND lease_type='operating' AND ladder_year='ImputedInterest'"
        ).fetchone()
        assert float(row[0]) == -2460.0
        # Finance lease liability row landed
        row = conn.execute(
            "SELECT amount FROM lease_commitments "
            "WHERE ticker='GOOG' AND lease_type='finance' AND ladder_year='LeaseLiability'"
        ).fetchone()
        assert float(row[0]) == 1677.0
    finally:
        conn.close()


def test_extract_logs_extractions_when_filing_doc_id_present(
    db: Path, tmp_path: Path
) -> None:
    payload = {"Leases - Future Minimum Lease P": _goog_lease_section()}
    outcome = lc.extract(
        ticker="GOOG", fiscal_year=2024, fmp_payload=payload, sec_text=None,
        db_path=db, repo_root=tmp_path, filing_doc_id=99, as_of_date=date(2024, 12, 31),
    )
    assert outcome.n_extractions_logged == 18
    conn = sqlite3.connect(str(db))
    try:
        n_ext = conn.execute(
            "SELECT COUNT(*) FROM extractions WHERE extractor_id = ?", (lc.EXTRACTOR_ID,)
        ).fetchone()[0]
        assert n_ext == 18
    finally:
        conn.close()


def test_extract_is_idempotent(db: Path, tmp_path: Path) -> None:
    payload = {"Leases - Future Minimum Lease P": _goog_lease_section()}
    # First run
    outcome1 = lc.extract(
        ticker="GOOG", fiscal_year=2024, fmp_payload=payload, sec_text=None,
        db_path=db, repo_root=tmp_path, filing_doc_id=1, as_of_date=date(2024, 12, 31),
    )
    assert outcome1.n_rows_inserted == 18
    # Second run with same inputs — every insert should hit the unique
    # constraint and be skipped.
    outcome2 = lc.extract(
        ticker="GOOG", fiscal_year=2024, fmp_payload=payload, sec_text=None,
        db_path=db, repo_root=tmp_path, filing_doc_id=1, as_of_date=date(2024, 12, 31),
    )
    assert outcome2.n_rows_proposed == 18
    assert outcome2.n_rows_inserted == 0
    conn = sqlite3.connect(str(db))
    try:
        n = conn.execute("SELECT COUNT(*) FROM lease_commitments").fetchone()[0]
        assert n == 18
    finally:
        conn.close()


def test_extract_returns_no_data_when_section_missing(
    db: Path, tmp_path: Path
) -> None:
    payload = {
        "symbol": "FAKETICK",
        "Revenues": [{"unrelated": ["section"]}],
    }
    outcome = lc.extract(
        ticker="FAKETICK", fiscal_year=2024, fmp_payload=payload, sec_text=None,
        db_path=db, repo_root=tmp_path,
    )
    assert outcome.status == "no_data"
    assert outcome.n_rows_inserted == 0


def test_extract_returns_no_data_when_fy_not_in_periods(
    db: Path, tmp_path: Path
) -> None:
    # Payload says fy2024 but caller asks for fy2030 → no matching column.
    payload = {"Leases - Future Minimum Lease P": _goog_lease_section()}
    outcome = lc.extract(
        ticker="GOOG", fiscal_year=2030, fmp_payload=payload, sec_text=None,
        db_path=db, repo_root=tmp_path,
    )
    assert outcome.status == "no_data"
    assert "no period column matched" in outcome.notes


def test_extract_handles_missing_payload(db: Path, tmp_path: Path) -> None:
    outcome = lc.extract(
        ticker="X", fiscal_year=2024, fmp_payload=None, sec_text=None,
        db_path=db, repo_root=tmp_path,
    )
    assert outcome.status == "no_data"


def test_extract_handles_missing_fiscal_year(db: Path, tmp_path: Path) -> None:
    payload = {"Leases - Future Minimum Lease P": _goog_lease_section()}
    outcome = lc.extract(
        ticker="GOOG", fiscal_year=None, fmp_payload=payload, sec_text=None,
        db_path=db, repo_root=tmp_path,
    )
    assert outcome.status == "no_data"


def test_lease_type_classifier_handles_capital_lease_legacy_naming() -> None:
    # Older filings sometimes used "Capital Leases" instead of "Finance Leases".
    assert lc._classify_lease_type(["Operating Leases"]) == "operating"
    assert lc._classify_lease_type(["Finance Leases"]) == "finance"
    assert lc._classify_lease_type(["Capital Leases"]) == "finance"
    assert lc._classify_lease_type(["Something else entirely"]) is None
    assert lc._classify_lease_type([]) is None


def test_ladder_label_classifier_handles_year_offsets() -> None:
    # FY2024 → '2025' is Y1, '2029' is Y5, '2030' is Thereafter
    assert lc._classify_label("2025", fiscal_year=2024) == ("Y1", 2025)
    assert lc._classify_label("2029", fiscal_year=2024) == ("Y5", 2029)
    assert lc._classify_label("2030", fiscal_year=2024) == ("Thereafter", 2030)
    assert lc._classify_label("Thereafter", fiscal_year=2024) == ("Thereafter", None)
    assert lc._classify_label("Total future lease payments", fiscal_year=2024) == ("TotalPayments", None)
    assert lc._classify_label("Less imputed interest", fiscal_year=2024) == ("ImputedInterest", None)
    assert lc._classify_label("Total lease liability balance", fiscal_year=2024) == ("LeaseLiability", None)
    assert lc._classify_label("Unrelated noise label", fiscal_year=2024) == (None, None)


def test_extract_handles_operating_only_section_without_axis_marker(
    db: Path, tmp_path: Path
) -> None:
    """VEEV-style: section is operating-only, no Operating Leases axis
    marker, labels are 'Fiscal YYYY' instead of bare year."""
    payload: dict[str, object] = {
        "Leases- Schedule of Maturities ": [
            {"Leases- Schedule of Maturities of Lease Liabilities (Details) $ in Thousands":
                ["Jan. 31, 2024 USD ($)"]},
            {"Lessee, Operating Lease, Liability, Payment, Due [Abstract]": ["\xa0"]},
            {"Fiscal 2025": [10213]},
            {"Fiscal 2026": [10710]},
            {"Fiscal 2027": [9798]},
            {"Fiscal 2028": [9116]},
            {"Fiscal 2029": [6553]},
            {"Thereafter": [19348]},
            {"Total operating lease payments": [65738]},
            {"Less imputed interest": [9963]},
            {"Total operating lease liabilities": [55775]},
        ],
    }
    outcome = lc.extract(
        ticker="VEEV", fiscal_year=2024, fmp_payload=payload, sec_text=None,
        db_path=db, repo_root=tmp_path,
    )
    assert outcome.status == "ok"
    # 5 ladder years + Thereafter + TotalPayments + ImputedInterest + LeaseLiability = 9
    assert outcome.n_rows_inserted == 9
    conn = sqlite3.connect(str(db))
    try:
        n_op = conn.execute(
            "SELECT COUNT(*) FROM lease_commitments WHERE ticker='VEEV' AND lease_type='operating'"
        ).fetchone()[0]
        n_fin = conn.execute(
            "SELECT COUNT(*) FROM lease_commitments WHERE ticker='VEEV' AND lease_type='finance'"
        ).fetchone()[0]
        assert n_op == 9
        assert n_fin == 0
        # 'Fiscal 2025' becomes Y1 with calendar_year=2025
        row = conn.execute(
            "SELECT amount, ladder_calendar_year FROM lease_commitments "
            "WHERE ticker='VEEV' AND lease_type='operating' AND ladder_year='Y1'"
        ).fetchone()
        assert row == (10213, 2025)
        # Currency parses as USD; scale parses as 'thousands' from '$ in Thousands'
        row = conn.execute(
            "SELECT currency, unit FROM lease_commitments WHERE ticker='VEEV' LIMIT 1"
        ).fetchone()
        assert row == ("USD", "thousands")
    finally:
        conn.close()


def test_classify_label_accepts_fiscal_prefix() -> None:
    assert lc._classify_label("Fiscal 2025", fiscal_year=2024) == ("Y1", 2025)
    assert lc._classify_label("Fiscal year 2026", fiscal_year=2024) == ("Y2", 2026)
    assert lc._classify_label("Year 2027", fiscal_year=2024) == ("Y3", 2027)


def test_classify_label_accepts_extended_total_labels() -> None:
    # VEEV-style labels with embedded 'operating'
    assert lc._classify_label(
        "Total operating lease payments", fiscal_year=2024
    ) == ("TotalPayments", None)
    assert lc._classify_label(
        "Total operating lease liabilities", fiscal_year=2024
    ) == ("LeaseLiability", None)


def test_infer_fallback_lease_type_from_title() -> None:
    assert lc._infer_fallback_lease_type(
        "Lessee, Operating Lease, Liability — USD ($)", rows=[]
    ) == "operating"
    assert lc._infer_fallback_lease_type(
        "Finance Lease Future Payments — USD ($)", rows=[]
    ) == "finance"
    # Mixed title → no inference
    assert lc._infer_fallback_lease_type(
        "Operating Lease and Finance Lease Maturities", rows=[]
    ) is None


def test_infer_as_of_handles_common_formats() -> None:
    assert lc._infer_as_of("Dec. 31, 2024", fiscal_year=2024) == date(2024, 12, 31)
    assert lc._infer_as_of("Dec 31, 2024", fiscal_year=2024) == date(2024, 12, 31)
    assert lc._infer_as_of("December 31, 2024", fiscal_year=2024) == date(2024, 12, 31)
    # Fallback for unrecognized formats
    assert lc._infer_as_of("?? ?? ????", fiscal_year=2024) == date(2024, 12, 31)
