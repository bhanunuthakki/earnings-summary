# pyright: reportPrivateUsage=false
# This test drives audit_segment_quarterly_coverage's internal _audit_ticker
# directly -- the same convention tests/test_segment_quarterly_10q.py uses
# for compute.segment_quarterly_10q's private helpers.
"""Tests for execution/audit_segment_quarterly_coverage.py -- the per-ticker
x per-quarter segment coverage matrix (docs/design/
segment_quarterly_framework.md §5.2, Phase 2).

Covers: reported/derived detection from segment_periods, source_missing with
a reason code when nothing's captured yet, the fpi_route_unproven gate for
non-10-K filing regimes, the legacy-quarterly-cache-gone-annual-only
detector (Finding 1), and that surfacing an existing not_computable/
tolerance_breach row (written by the deriver) doesn't get clobbered by a
fresh source_missing guess.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import audit_segment_quarterly_coverage as audit  # noqa: E402

_SCHEMA = """
CREATE TABLE tracked_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL UNIQUE,
    filing_regime TEXT,
    fiscal_year_end TEXT,
    list_type TEXT
);
CREATE TABLE segment_periods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    period_end DATETIME NOT NULL,
    fiscal_period_type VARCHAR(8) NOT NULL,
    source_doc_id INTEGER NOT NULL,
    unit VARCHAR(16) NOT NULL,
    period_basis VARCHAR(16) NOT NULL DEFAULT 'discrete'
);
CREATE TABLE segment_quarterly_coverage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    period_end DATETIME NOT NULL,
    fiscal_period_type VARCHAR(8) NOT NULL,
    dim_type VARCHAR(16),
    dim_name VARCHAR(128),
    status VARCHAR(24) NOT NULL,
    reason_code VARCHAR(64),
    source_doc_id INTEGER,
    method_version VARCHAR(32),
    checked_at DATETIME NOT NULL
);
"""


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    c.commit()
    return c


def _insert_company(
    conn: sqlite3.Connection, ticker: str, *, filing_regime: str | None, fye: str | None
) -> None:
    conn.execute(
        "INSERT INTO tracked_companies (ticker, filing_regime, fiscal_year_end, list_type) "
        "VALUES (?, ?, ?, 'portfolio')",
        (ticker, filing_regime, fye),
    )
    conn.commit()


def _write_dates_file(repo_root: Path, ticker: str, entries: list[dict[str, object]]) -> None:
    out_dir = repo_root / "data" / "historical" / "fmp"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{ticker}_financial_reports_dates.json").write_text(
        json.dumps(entries), encoding="utf-8"
    )


def test_reported_from_segment_periods(tmp_path: Path, conn: sqlite3.Connection) -> None:
    ticker = "TESTCO"
    _insert_company(conn, ticker, filing_regime="10-K", fye="12-31")
    _write_dates_file(tmp_path, ticker, [{"symbol": ticker, "fiscalYear": 2025, "period": "Q1"}])
    conn.execute(
        "INSERT INTO segment_periods (ticker, period_end, fiscal_period_type, source_doc_id, unit) "
        "VALUES (?, '2025-03-31', 'Q1', 1, 'actual')",
        (ticker,),
    )
    conn.commit()

    out = audit._audit_ticker(conn, tmp_path, ticker, None)
    assert out["2025_Q1"]["status"] == "reported"


def test_derived_period_basis_reported_as_derived(tmp_path: Path, conn: sqlite3.Connection) -> None:
    ticker = "TESTCO"
    _insert_company(conn, ticker, filing_regime="10-K", fye="12-31")
    _write_dates_file(tmp_path, ticker, [{"symbol": ticker, "fiscalYear": 2025, "period": "Q2"}])
    conn.execute(
        "INSERT INTO segment_periods "
        "(ticker, period_end, fiscal_period_type, source_doc_id, unit, period_basis) "
        "VALUES (?, '2025-06-30', 'Q2', 1, 'actual', 'derived')",
        (ticker,),
    )
    conn.commit()

    out = audit._audit_ticker(conn, tmp_path, ticker, None)
    assert out["2025_Q2"]["status"] == "derived"


def test_source_missing_when_nothing_captured(tmp_path: Path, conn: sqlite3.Connection) -> None:
    ticker = "TESTCO"
    _insert_company(conn, ticker, filing_regime="10-K", fye="12-31")
    _write_dates_file(tmp_path, ticker, [{"symbol": ticker, "fiscalYear": 2025, "period": "Q3"}])

    out = audit._audit_ticker(conn, tmp_path, ticker, None)
    assert out["2025_Q3"]["status"] == "source_missing"
    assert out["2025_Q3"]["reason_code"] == "no_10q_json_fetched"
    # A coverage row was written by the audit itself.
    row = conn.execute(
        "SELECT status, reason_code FROM segment_quarterly_coverage "
        "WHERE ticker = ? AND fiscal_period_type = 'Q3'",
        (ticker,),
    ).fetchone()
    assert row["status"] == "source_missing"


def test_fy_expands_to_fy_and_q4(tmp_path: Path, conn: sqlite3.Connection) -> None:
    ticker = "TESTCO"
    _insert_company(conn, ticker, filing_regime="10-K", fye="12-31")
    _write_dates_file(tmp_path, ticker, [{"symbol": ticker, "fiscalYear": 2025, "period": "FY"}])

    out = audit._audit_ticker(conn, tmp_path, ticker, None)
    assert "2025_FY" in out
    assert "2025_Q4" in out
    assert out["2025_Q4"]["reason_code"] == "no_10k_fy_anchor_or_quarterly_inputs"


def test_fpi_route_unproven_for_20f(tmp_path: Path, conn: sqlite3.Connection) -> None:
    ticker = "TESTCO"
    _insert_company(conn, ticker, filing_regime="20-F", fye="12-31")
    _write_dates_file(tmp_path, ticker, [{"symbol": ticker, "fiscalYear": 2025, "period": "Q1"}])

    out = audit._audit_ticker(conn, tmp_path, ticker, None)
    assert out["2025_Q1"]["status"] == "source_missing"
    assert out["2025_Q1"]["reason_code"] == "fpi_route_unproven"


def test_filing_regime_unresolved_distinct_from_fpi_route_unproven(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    """A ticker with filing_regime still NULL (the §1.3 self-heal hasn't run
    yet) must NOT be mislabeled fpi_route_unproven -- that reason code is
    reserved for a CONFIRMED 20-F/40-F filer. Caught by a real-data smoke
    test against AGX (a plain 10-K filer with filing_regime NULL on the live
    DB, since it predates the 28-name hand backfill)."""
    ticker = "TESTCO"
    _insert_company(conn, ticker, filing_regime=None, fye="12-31")
    _write_dates_file(tmp_path, ticker, [{"symbol": ticker, "fiscalYear": 2025, "period": "Q1"}])

    out = audit._audit_ticker(conn, tmp_path, ticker, None)
    assert out["2025_Q1"]["status"] == "source_missing"
    assert out["2025_Q1"]["reason_code"] == "filing_regime_unresolved"


def test_existing_not_computable_row_surfaced_as_is(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    """A not_computable row already written by the deriver must be surfaced
    verbatim, not overwritten by the audit's own source_missing guess."""
    ticker = "TESTCO"
    _insert_company(conn, ticker, filing_regime="10-K", fye="12-31")
    _write_dates_file(tmp_path, ticker, [{"symbol": ticker, "fiscalYear": 2025, "period": "FY"}])
    conn.execute(
        "INSERT INTO segment_quarterly_coverage "
        "(ticker, period_end, fiscal_period_type, dim_type, dim_name, status, reason_code, "
        " source_doc_id, method_version, checked_at) "
        "VALUES (?, '2025-12-31', 'Q4', NULL, NULL, 'not_computable', "
        " 'missing_prior_anchor_for_subtraction', NULL, 'segment_q4_derive_v1', CURRENT_TIMESTAMP)",
        (ticker,),
    )
    conn.commit()

    out = audit._audit_ticker(conn, tmp_path, ticker, None)
    assert out["2025_Q4"]["status"] == "not_computable"
    assert out["2025_Q4"]["reason_code"] == "missing_prior_anchor_for_subtraction"


def test_legacy_endpoint_annual_only_detected(tmp_path: Path, conn: sqlite3.Connection) -> None:
    ticker = "TESTCO"
    _insert_company(conn, ticker, filing_regime="10-K", fye="12-31")
    _write_dates_file(tmp_path, ticker, [{"symbol": ticker, "fiscalYear": 2025, "period": "Q1"}])
    out_dir = tmp_path / "data" / "historical" / "fmp"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{ticker}_product_segments_quarterly.json").write_text(
        json.dumps(
            [
                {"symbol": ticker, "fiscalYear": 2025, "period": "FY"},
                {"symbol": ticker, "fiscalYear": 2024, "period": "FY"},
                {"symbol": ticker, "fiscalYear": 2023, "period": "Q4"},
            ]
        ),
        encoding="utf-8",
    )

    out = audit._audit_ticker(conn, tmp_path, ticker, None)
    assert out["2025_Q1"]["reason_code"] in ("legacy_endpoint_annual_only", "legacy_cache_stale")


def test_since_year_filters_older_fiscal_years(tmp_path: Path, conn: sqlite3.Connection) -> None:
    ticker = "TESTCO"
    _insert_company(conn, ticker, filing_regime="10-K", fye="12-31")
    _write_dates_file(
        tmp_path,
        ticker,
        [
            {"symbol": ticker, "fiscalYear": 2020, "period": "Q1"},
            {"symbol": ticker, "fiscalYear": 2025, "period": "Q1"},
        ],
    )
    out = audit._audit_ticker(conn, tmp_path, ticker, 2023)
    assert "2020_Q1" not in out
    assert "2025_Q1" in out
