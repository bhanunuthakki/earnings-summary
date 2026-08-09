# pyright: reportPrivateUsage=false
# This test drives fmp_doc_index's internal helpers (_classify_regime_from_
# forms, _fetch_sec_regime) directly, same convention test_generic_xbrl_
# capture.py uses.
"""Unit tests for pipeline.fmp_doc_index filing_regime self-heal
(segment_quarterly_framework.md §1.3) — mirrors
test_set_instrument_type_from_fmp.py's structure for the sibling function.

Never hits the real network: _fetch_sec_regime is monkeypatched per-test so
these tests are hermetic and fast; a dedicated test below covers its own
response-shape parsing directly.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import pipeline.fmp_doc_index as fmp_doc_index  # noqa: E402
from net.client import HttpCallError, HttpErrorKind, HttpJsonResponse  # noqa: E402
from pipeline.fmp_doc_index import (  # noqa: E402
    classify_filing_regime_from_profile,
    set_filing_regime_from_profile,
)

# ---------------------------------------------------------------------------
# Pure classifiers
# ---------------------------------------------------------------------------


def test_classify_from_forms_most_recent_wins() -> None:
    assert fmp_doc_index._classify_regime_from_forms(["8-K", "10-K", "10-Q"]) == "10-K"


def test_classify_from_forms_20f() -> None:
    assert fmp_doc_index._classify_regime_from_forms(["6-K", "20-F/A"]) == "20-F"


def test_classify_from_forms_none_found() -> None:
    assert fmp_doc_index._classify_regime_from_forms(["8-K", "6-K"]) is None


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ({}, "10-K"),
        ({"isAdr": True, "country": "CA"}, "40-F"),
        ({"isAdr": True, "country": "BR"}, "20-F"),
        ({"isAdr": False, "country": "CA"}, "10-K"),
        ({"isAdr": True, "country": "ca"}, "40-F"),  # case-insensitive
    ],
)
def test_classify_filing_regime_from_profile(profile: dict[str, object], expected: str) -> None:
    assert classify_filing_regime_from_profile(profile) == expected


# ---------------------------------------------------------------------------
# DB-writing setter
# ---------------------------------------------------------------------------


def _make_db(db_path: Path, ticker: str, *, filing_regime: str | None) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE tracked_companies (ticker TEXT, filing_regime TEXT)")
    conn.execute(
        "INSERT INTO tracked_companies (ticker, filing_regime) VALUES (?, ?)",
        (ticker, filing_regime),
    )
    conn.commit()
    conn.close()


def _write_profile(project_root: Path, ticker: str, **flags: object) -> None:
    fmp_dir = project_root / "data" / "historical" / "fmp"
    fmp_dir.mkdir(parents=True, exist_ok=True)
    record = {"symbol": ticker, **flags}
    (fmp_dir / f"{ticker}_profile.json").write_text(json.dumps([record]), encoding="utf-8")


def _open(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _regime_in_db(db_path: Path, ticker: str) -> str | None:
    conn = _open(db_path)
    row = conn.execute(
        "SELECT filing_regime FROM tracked_companies WHERE ticker = ?", (ticker,)
    ).fetchone()
    conn.close()
    return row["filing_regime"] if row else None


def _fake_sec_regime_10k(cik: str) -> str | None:
    return "10-K"


def _fake_sec_regime_none(cik: str) -> str | None:
    return None


def test_sec_signal_wins_when_cik_resolves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ground-truth SEC signal wins even when the FMP profile would suggest
    something else — never consulted once SEC resolves."""
    db = tmp_path / "p.db"
    _make_db(db, "AMZN", filing_regime=None)
    _write_profile(tmp_path, "AMZN", isAdr=False, country="US")
    monkeypatch.setattr(fmp_doc_index, "_fetch_sec_regime", _fake_sec_regime_10k)

    conn = _open(db)
    result = set_filing_regime_from_profile(conn, "AMZN", tmp_path)
    conn.close()

    assert result == "10-K"
    assert _regime_in_db(db, "AMZN") == "10-K"


def test_falls_back_to_fmp_profile_when_sec_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "p.db"
    _make_db(db, "DLO", filing_regime=None)
    _write_profile(tmp_path, "DLO", isAdr=True, country="BR")
    # DLO isn't in CIK_MAP, or the SEC fetch fails either way — force failure.
    monkeypatch.setattr(fmp_doc_index, "_fetch_sec_regime", _fake_sec_regime_none)

    conn = _open(db)
    result = set_filing_regime_from_profile(conn, "DLO", tmp_path)
    conn.close()

    assert result == "20-F"
    assert _regime_in_db(db, "DLO") == "20-F"


def test_does_not_clobber_curated_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "p.db"
    _make_db(db, "NU", filing_regime="20-F")
    _write_profile(tmp_path, "NU", isAdr=False, country="US")  # would classify 10-K
    monkeypatch.setattr(fmp_doc_index, "_fetch_sec_regime", _fake_sec_regime_10k)

    conn = _open(db)
    result = set_filing_regime_from_profile(conn, "NU", tmp_path)
    conn.close()

    assert result == "20-F"  # reports the persisted (curated) value, unchanged
    assert _regime_in_db(db, "NU") == "20-F"


def test_no_signal_resolves_leaves_null(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "p.db"
    _make_db(db, "GHOST", filing_regime=None)
    # No profile JSON, no SEC signal.
    monkeypatch.setattr(fmp_doc_index, "_fetch_sec_regime", _fake_sec_regime_none)

    conn = _open(db)
    result = set_filing_regime_from_profile(conn, "GHOST", tmp_path)
    conn.close()

    assert result is None
    assert _regime_in_db(db, "GHOST") is None


def test_no_tracked_row_returns_none(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE tracked_companies (ticker TEXT, filing_regime TEXT)")
    conn.commit()
    conn.close()

    conn = _open(db)
    result = set_filing_regime_from_profile(conn, "ORPH", tmp_path)
    conn.close()

    assert result is None


def _fake_request_json(*a: object, **k: object) -> HttpJsonResponse:
    return HttpJsonResponse(
        status_code=200,
        payload={"filings": {"recent": {"form": ["8-K", "10-K", "10-Q"]}}},
    )


def test_fetch_sec_regime_parses_real_response_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """A minimal, real submissions.json shape resolves via the actual HTTP
    parsing path (network itself mocked at the shared-client boundary)."""
    monkeypatch.setattr(fmp_doc_index.HTTP_CLIENT, "request_json", _fake_request_json)
    assert fmp_doc_index._fetch_sec_regime("0001018724") == "10-K"


def test_fetch_sec_regime_degrades_on_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*a: object, **k: object) -> object:
        raise HttpCallError(
            kind=HttpErrorKind.NETWORK,
            message="boom",
            retryable=True,
        )

    monkeypatch.setattr(fmp_doc_index.HTTP_CLIENT, "request_json", _raise)
    assert fmp_doc_index._fetch_sec_regime("0000000000") is None
