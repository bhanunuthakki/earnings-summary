"""Unit tests for pipeline.fmp_doc_index instrument_type derivation.

Covers the Issue-2 fix: onboard_ticker (and raw-SQL inserts) used to leave
tracked_companies.instrument_type NULL, tripping onboard_pending_tickers'
`no_instrument_type` reason forever. set_instrument_type_from_fmp classifies it
from the cached FMP profile, but only when NULL so curated values aren't lost.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pipeline.fmp_doc_index import (  # noqa: E402
    classify_instrument_type_from_profile,
    set_instrument_type_from_fmp,
)

# ---------------------------------------------------------------------------
# Pure classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ({}, "equity"),
        ({"isEtf": False, "isAdr": False, "isFund": False}, "equity"),
        ({"isAdr": True}, "adr"),
        ({"isEtf": True}, "etf"),
        ({"isFund": True}, "etf"),
        # ETF/fund wins over a stray isAdr.
        ({"isEtf": True, "isAdr": True}, "etf"),
        # v3-fallback string booleans.
        ({"isAdr": "true"}, "adr"),
        ({"isEtf": "false", "isAdr": "false"}, "equity"),
        ({"isEtf": "true"}, "etf"),
    ],
)
def test_classify_instrument_type_from_profile(profile, expected) -> None:
    assert classify_instrument_type_from_profile(profile) == expected


# ---------------------------------------------------------------------------
# DB-writing setter
# ---------------------------------------------------------------------------


def _make_db(db_path: Path, ticker: str, *, instrument_type: str | None) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE tracked_companies "
        "(ticker TEXT, name TEXT, list_type TEXT, instrument_type TEXT)"
    )
    conn.execute(
        "INSERT INTO tracked_companies (ticker, name, list_type, instrument_type) "
        "VALUES (?, ?, 'evaluation', ?)",
        (ticker, f"{ticker} Inc", instrument_type),
    )
    conn.commit()
    conn.close()


def _write_profile(project_root: Path, ticker: str, **flags: object) -> None:
    fmp_dir = project_root / "data" / "historical" / "fmp"
    fmp_dir.mkdir(parents=True, exist_ok=True)
    record = {"symbol": ticker, "companyName": f"{ticker} Inc", **flags}
    (fmp_dir / f"{ticker}_profile.json").write_text(
        json.dumps([record]), encoding="utf-8"
    )


def _open(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _instrument_in_db(db_path: Path, ticker: str) -> str | None:
    conn = _open(db_path)
    row = conn.execute(
        "SELECT instrument_type FROM tracked_companies WHERE ticker = ?", (ticker,)
    ).fetchone()
    conn.close()
    return row["instrument_type"] if row else None


def test_sets_equity_for_plain_us_company(tmp_path: Path) -> None:
    """FRVO acceptance: a US IPO profile (no ADR/ETF flags) classifies equity."""
    db = tmp_path / "p.db"
    _make_db(db, "FRVO", instrument_type=None)
    _write_profile(tmp_path, "FRVO", isEtf=False, isAdr=False, country="US")

    conn = _open(db)
    result = set_instrument_type_from_fmp(conn, "FRVO", tmp_path)
    conn.close()

    assert result == "equity"
    assert _instrument_in_db(db, "FRVO") == "equity"


def test_sets_adr_from_profile_flag(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    _make_db(db, "DLO", instrument_type=None)
    _write_profile(tmp_path, "DLO", isAdr=True)

    conn = _open(db)
    result = set_instrument_type_from_fmp(conn, "DLO", tmp_path)
    conn.close()

    assert result == "adr"
    assert _instrument_in_db(db, "DLO") == "adr"


def test_sets_etf_from_profile_flag(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    _make_db(db, "FLKR", instrument_type=None)
    _write_profile(tmp_path, "FLKR", isEtf=True)

    conn = _open(db)
    result = set_instrument_type_from_fmp(conn, "FLKR", tmp_path)
    conn.close()

    assert result == "etf"
    assert _instrument_in_db(db, "FLKR") == "etf"


def test_does_not_clobber_curated_value(tmp_path: Path) -> None:
    """Already-classified rows are left alone even if the FMP profile disagrees
    (e.g. NU is curated 'adr' but FMP may not flag isAdr)."""
    db = tmp_path / "p.db"
    _make_db(db, "NU", instrument_type="adr")
    _write_profile(tmp_path, "NU", isEtf=False, isAdr=False)  # would classify equity

    conn = _open(db)
    result = set_instrument_type_from_fmp(conn, "NU", tmp_path)
    conn.close()

    assert result == "adr"  # reports the persisted (curated) value
    assert _instrument_in_db(db, "NU") == "adr"


def test_missing_profile_returns_none_and_leaves_null(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    _make_db(db, "GHOST", instrument_type=None)
    # No profile JSON written.

    conn = _open(db)
    result = set_instrument_type_from_fmp(conn, "GHOST", tmp_path)
    conn.close()

    assert result is None
    assert _instrument_in_db(db, "GHOST") is None


def test_no_tracked_row_returns_none(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    # Table exists but no row for this ticker.
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE tracked_companies "
        "(ticker TEXT, name TEXT, list_type TEXT, instrument_type TEXT)"
    )
    conn.commit()
    conn.close()
    _write_profile(tmp_path, "ORPH", isAdr=False, isEtf=False)

    conn = _open(db)
    result = set_instrument_type_from_fmp(conn, "ORPH", tmp_path)
    conn.close()

    assert result is None
