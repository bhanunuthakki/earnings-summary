"""Tests for compute.segment_q4_derive -- Q4 = FY - (Q1+Q2+Q3) segment
derivation (docs/design/segment_quarterly_framework.md §3, Phase 2).

Covers: the clean derivation case, a missing-quarter guard (not_computable),
the sign-sanity tolerance-breach guard, a recast/supersede chain (a later FY
filing with a different segment value re-derives and chains), the bounded
recast-propagation guard (a "recast" reaching further back than a single
filing's comparative window is refused, not silently guessed), an
unmatched-segment-identity gap, and idempotency on a same-inputs re-run.
"""

from __future__ import annotations

import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import compute.segment_q4_derive as q4  # noqa: E402

_SCHEMA = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    source_type TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    period_end DATETIME,
    fetched_at TIMESTAMP NOT NULL,
    fetch_status TEXT NOT NULL,
    raw_bytes_size INTEGER NOT NULL
);
CREATE TABLE tracked_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL UNIQUE,
    fiscal_year_end TEXT
);
CREATE TABLE segment_periods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    period_end DATETIME NOT NULL,
    fiscal_period_type VARCHAR(8) NOT NULL,
    source_doc_id INTEGER NOT NULL REFERENCES documents(id),
    currency VARCHAR(8),
    unit VARCHAR(16) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    period_basis VARCHAR(16) NOT NULL DEFAULT 'discrete',
    raw_period_label TEXT,
    method_version VARCHAR(32)
);
CREATE TABLE segment_dimensions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id INTEGER NOT NULL REFERENCES segment_periods(id),
    dim_type VARCHAR(16) NOT NULL,
    dim_name VARCHAR(128) NOT NULL,
    value NUMERIC(20, 4) NOT NULL,
    metric VARCHAR(32) NOT NULL,
    unit VARCHAR(16),
    disclosure_status VARCHAR(16) NOT NULL DEFAULT 'reported',
    method_version VARCHAR(32),
    confidence REAL NOT NULL DEFAULT 1.0,
    extracted_by VARCHAR(64),
    locator TEXT,
    derived_from TEXT,
    supersedes_id INTEGER,
    segment_entity_id INTEGER
);
CREATE TABLE financial_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    period_end DATETIME NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    line_item TEXT NOT NULL,
    value NUMERIC NOT NULL
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


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _create_schema(c)
    return c


def _insert_document(
    conn: sqlite3.Connection,
    *,
    doc_id: int,
    ticker: str,
    doc_type: str,
    period_end: str,
    fetched_at: str,
) -> None:
    conn.execute(
        "INSERT INTO documents "
        "(id, ticker, source_type, doc_type, file_path, sha256, period_end, fetched_at, "
        " fetch_status, raw_bytes_size) "
        "VALUES (?, ?, 'fmp', ?, ?, ?, ?, ?, 'ok', 1)",
        (doc_id, ticker, doc_type, f"{ticker}_{doc_id}.json", "0" * 64, period_end, fetched_at),
    )
    conn.commit()


def _insert_period(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    period_end: str,
    fiscal_period_type: str,
    source_doc_id: int,
    period_basis: str = "discrete",
) -> int:
    cur = conn.execute(
        "INSERT INTO segment_periods "
        "(ticker, period_end, fiscal_period_type, source_doc_id, unit, period_basis) "
        "VALUES (?, ?, ?, ?, 'actual', ?)",
        (ticker, period_end, fiscal_period_type, source_doc_id, period_basis),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


def _insert_dim(
    conn: sqlite3.Connection,
    *,
    period_id: int,
    dim_name: str,
    value: str,
    metric: str = "revenue",
    dim_type: str = "business_unit",
    disclosure_status: str = "reported",
    method_version: str | None = None,
    confidence: float = 1.0,
) -> int:
    cur = conn.execute(
        "INSERT INTO segment_dimensions "
        "(period_id, dim_type, dim_name, value, metric, disclosure_status, method_version, confidence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            period_id,
            dim_type,
            dim_name,
            value,
            metric,
            disclosure_status,
            method_version,
            confidence,
        ),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


def _seed_clean_fy2025(conn: sqlite3.Connection, ticker: str = "TESTCO") -> None:
    _insert_document(
        conn,
        doc_id=1,
        ticker=ticker,
        doc_type="fmp_10q_json",
        period_end="2025-03-31",
        fetched_at="2025-04-15",
    )
    _insert_document(
        conn,
        doc_id=2,
        ticker=ticker,
        doc_type="fmp_10q_json",
        period_end="2025-06-30",
        fetched_at="2025-07-15",
    )
    _insert_document(
        conn,
        doc_id=3,
        ticker=ticker,
        doc_type="fmp_10q_json",
        period_end="2025-09-30",
        fetched_at="2025-10-15",
    )
    _insert_document(
        conn,
        doc_id=4,
        ticker=ticker,
        doc_type="fmp_10k_json",
        period_end="2025-12-31",
        fetched_at="2026-02-15",
    )

    p1 = _insert_period(
        conn, ticker=ticker, period_end="2025-03-31", fiscal_period_type="Q1", source_doc_id=1
    )
    _insert_dim(conn, period_id=p1, dim_name="North America", value="1000000000")

    p2 = _insert_period(
        conn,
        ticker=ticker,
        period_end="2025-06-30",
        fiscal_period_type="Q2",
        source_doc_id=2,
        period_basis="derived",
    )
    _insert_dim(
        conn,
        period_id=p2,
        dim_name="North America",
        value="1200000000",
        disclosure_status="derived",
        method_version="segment_q2q3_derive_v1",
        confidence=0.97,
    )

    p3 = _insert_period(
        conn,
        ticker=ticker,
        period_end="2025-09-30",
        fiscal_period_type="Q3",
        source_doc_id=3,
        period_basis="derived",
    )
    _insert_dim(
        conn,
        period_id=p3,
        dim_name="North America",
        value="1000000000",
        disclosure_status="derived",
        method_version="segment_q2q3_derive_v1",
        confidence=0.97,
    )

    p4 = _insert_period(
        conn, ticker=ticker, period_end="2025-12-31", fiscal_period_type="FY", source_doc_id=4
    )
    _insert_dim(conn, period_id=p4, dim_name="North America", value="4400000000")


# ---------------------------------------------------------------------------
# Clean case
# ---------------------------------------------------------------------------


def test_clean_derivation(tmp_path: Path, conn: sqlite3.Connection) -> None:
    _seed_clean_fy2025(conn)
    result = q4.derive_for_ticker("TESTCO", 2025, tmp_path, conn)

    assert result.derived_inserted == 1
    assert result.not_computable_count == 0
    row = conn.execute(
        "SELECT sp.fiscal_period_type, sp.period_basis, sd.value, sd.disclosure_status, "
        "sd.method_version, sd.confidence, sd.derived_from "
        "FROM segment_dimensions sd JOIN segment_periods sp ON sp.id = sd.period_id "
        "WHERE sd.method_version = 'segment_q4_derive_v1'"
    ).fetchone()
    assert row["fiscal_period_type"] == "Q4"
    assert row["period_basis"] == "derived"
    # 4400M - (1000M + 1200M + 1000M) = 1200M
    assert Decimal(str(row["value"])) == Decimal("1200000000")
    assert row["disclosure_status"] == "derived"
    # hops = 1 (own subtraction) + 0 (Q1 reported) + 1 (Q2 derived) + 1 (Q3 derived) = 3;
    # min input confidence is Q2/Q3's own 0.97 (Phase 1's one-hop decay), not 1.0.
    assert row["confidence"] == pytest.approx(0.97 * 0.97**3, rel=1e-6)
    assert "segment_dimension" in row["derived_from"]


def test_clean_derivation_is_idempotent(tmp_path: Path, conn: sqlite3.Connection) -> None:
    _seed_clean_fy2025(conn)
    first = q4.derive_for_ticker("TESTCO", 2025, tmp_path, conn)
    second = q4.derive_for_ticker("TESTCO", 2025, tmp_path, conn)
    assert first.derived_inserted == 1
    assert second.derived_inserted == 0  # same inputs -> no-op, not a duplicate row
    n_q4 = conn.execute(
        "SELECT COUNT(*) FROM segment_dimensions WHERE method_version = 'segment_q4_derive_v1'"
    ).fetchone()[0]
    assert n_q4 == 1


def test_balance_metric_is_refused_not_derived(tmp_path: Path, conn: sqlite3.Connection) -> None:
    """A point-in-time balance metric (non_current_assets, the fpi_6k
    geography-table companion column) must never be run through the
    FY - (Q1+Q2+Q3) subtraction -- three quarter-end snapshots subtracted
    from a year-end snapshot is a large negative artifact, not a Q4."""
    _seed_clean_fy2025(conn)
    # Add matching non_current_assets anchors on all four periods.
    for period_id in (1, 2, 3, 4):
        _insert_dim(
            conn,
            period_id=period_id,
            dim_name="North America",
            value="800000000",
            metric="non_current_assets",
        )
    result = q4.derive_for_ticker("TESTCO", 2025, tmp_path, conn)

    # Revenue still derives; the balance metric is refused with a coverage row.
    assert result.derived_inserted == 1
    assert result.reason_counts.get("balance_metric_not_derivable") == 1
    derived_metrics = {
        r["metric"]
        for r in conn.execute(
            "SELECT metric FROM segment_dimensions WHERE method_version = 'segment_q4_derive_v1'"
        ).fetchall()
    }
    assert derived_metrics == {"revenue"}
    cov = conn.execute(
        "SELECT status, reason_code FROM segment_quarterly_coverage "
        "WHERE reason_code = 'balance_metric_not_derivable'"
    ).fetchone()
    assert cov is not None
    assert cov["status"] == "not_computable"


# ---------------------------------------------------------------------------
# Missing-quarter guard
# ---------------------------------------------------------------------------


def test_missing_quarter_is_not_computable(tmp_path: Path, conn: sqlite3.Connection) -> None:
    ticker = "TESTCO"
    _insert_document(
        conn,
        doc_id=1,
        ticker=ticker,
        doc_type="fmp_10q_json",
        period_end="2025-03-31",
        fetched_at="2025-04-15",
    )
    _insert_document(
        conn,
        doc_id=4,
        ticker=ticker,
        doc_type="fmp_10k_json",
        period_end="2025-12-31",
        fetched_at="2026-02-15",
    )
    p1 = _insert_period(
        conn, ticker=ticker, period_end="2025-03-31", fiscal_period_type="Q1", source_doc_id=1
    )
    _insert_dim(conn, period_id=p1, dim_name="North America", value="1000000000")
    # Q2/Q3 entirely missing.
    p4 = _insert_period(
        conn, ticker=ticker, period_end="2025-12-31", fiscal_period_type="FY", source_doc_id=4
    )
    _insert_dim(conn, period_id=p4, dim_name="North America", value="4400000000")

    result = q4.derive_for_ticker(ticker, 2025, tmp_path, conn)

    assert result.derived_inserted == 0
    assert result.not_computable_count == 1
    n_q4 = conn.execute(
        "SELECT COUNT(*) FROM segment_dimensions WHERE method_version = 'segment_q4_derive_v1'"
    ).fetchone()[0]
    assert n_q4 == 0
    cov = conn.execute(
        "SELECT status, reason_code FROM segment_quarterly_coverage "
        "WHERE ticker = ? AND fiscal_period_type = 'Q4' AND dim_name = 'North America'",
        (ticker,),
    ).fetchone()
    assert cov["status"] == "not_computable"
    assert cov["reason_code"] == "missing_prior_anchor_for_subtraction"


def test_unmatched_segment_identity(tmp_path: Path, conn: sqlite3.Connection) -> None:
    """FY reports a segment with NO match at all in any quarter (a new/renamed
    segment, §3.1 point 3) -> unmatched_segment_identity, not the generic
    missing-anchor reason."""
    ticker = "TESTCO"
    _insert_document(
        conn,
        doc_id=1,
        ticker=ticker,
        doc_type="fmp_10q_json",
        period_end="2025-03-31",
        fetched_at="2025-04-15",
    )
    _insert_document(
        conn,
        doc_id=2,
        ticker=ticker,
        doc_type="fmp_10q_json",
        period_end="2025-06-30",
        fetched_at="2025-07-15",
    )
    _insert_document(
        conn,
        doc_id=3,
        ticker=ticker,
        doc_type="fmp_10q_json",
        period_end="2025-09-30",
        fetched_at="2025-10-15",
    )
    _insert_document(
        conn,
        doc_id=4,
        ticker=ticker,
        doc_type="fmp_10k_json",
        period_end="2025-12-31",
        fetched_at="2026-02-15",
    )
    for doc_id, ftype, pend in (
        (1, "Q1", "2025-03-31"),
        (2, "Q2", "2025-06-30"),
        (3, "Q3", "2025-09-30"),
    ):
        pid = _insert_period(
            conn, ticker=ticker, period_end=pend, fiscal_period_type=ftype, source_doc_id=doc_id
        )
        _insert_dim(conn, period_id=pid, dim_name="North America", value="100000000")
    p4 = _insert_period(
        conn, ticker=ticker, period_end="2025-12-31", fiscal_period_type="FY", source_doc_id=4
    )
    _insert_dim(conn, period_id=p4, dim_name="Brand New Segment", value="900000000")

    result = q4.derive_for_ticker(ticker, 2025, tmp_path, conn)

    assert result.derived_inserted == 0
    assert result.reason_counts.get("unmatched_segment_identity") == 1
    cov = conn.execute(
        "SELECT reason_code FROM segment_quarterly_coverage WHERE dim_name = 'Brand New Segment'"
    ).fetchone()
    assert cov["reason_code"] == "unmatched_segment_identity"


# ---------------------------------------------------------------------------
# Sign-sanity tolerance breach
# ---------------------------------------------------------------------------


def test_sign_sanity_tolerance_breach(tmp_path: Path, conn: sqlite3.Connection) -> None:
    ticker = "TESTCO"
    _insert_document(
        conn,
        doc_id=1,
        ticker=ticker,
        doc_type="fmp_10q_json",
        period_end="2025-03-31",
        fetched_at="2025-04-15",
    )
    _insert_document(
        conn,
        doc_id=2,
        ticker=ticker,
        doc_type="fmp_10q_json",
        period_end="2025-06-30",
        fetched_at="2025-07-15",
    )
    _insert_document(
        conn,
        doc_id=3,
        ticker=ticker,
        doc_type="fmp_10q_json",
        period_end="2025-09-30",
        fetched_at="2025-10-15",
    )
    _insert_document(
        conn,
        doc_id=4,
        ticker=ticker,
        doc_type="fmp_10k_json",
        period_end="2025-12-31",
        fetched_at="2026-02-15",
    )
    for doc_id, ftype, pend, val in (
        (1, "Q1", "2025-03-31", "1000000000"),
        (2, "Q2", "2025-06-30", "1000000000"),
        (3, "Q3", "2025-09-30", "1000000000"),
    ):
        pid = _insert_period(
            conn, ticker=ticker, period_end=pend, fiscal_period_type=ftype, source_doc_id=doc_id
        )
        _insert_dim(conn, period_id=pid, dim_name="North America", value=val)
    # FY total is LESS than the sum of the three quarters -> negative Q4.
    p4 = _insert_period(
        conn, ticker=ticker, period_end="2025-12-31", fiscal_period_type="FY", source_doc_id=4
    )
    _insert_dim(conn, period_id=p4, dim_name="North America", value="2500000000")

    result = q4.derive_for_ticker(ticker, 2025, tmp_path, conn)

    assert result.derived_inserted == 1
    assert result.tolerance_breach_count == 1
    row = conn.execute(
        "SELECT sd.value, sd.confidence FROM segment_dimensions sd "
        "WHERE sd.method_version = 'segment_q4_derive_v1'"
    ).fetchone()
    assert Decimal(str(row["value"])) == Decimal("-500000000")
    assert row["confidence"] == pytest.approx(0.3)
    cov = conn.execute(
        "SELECT status, reason_code FROM segment_quarterly_coverage "
        "WHERE fiscal_period_type = 'Q4' AND dim_name = 'North America' AND status = 'tolerance_breach'"
    ).fetchone()
    assert cov["reason_code"] == "negative_derived_value"


# ---------------------------------------------------------------------------
# Recast / supersede chain
# ---------------------------------------------------------------------------


def test_recast_supersedes_prior_derivation(tmp_path: Path, conn: sqlite3.Connection) -> None:
    """A later FY filing (e.g. the following year's 10-K, carrying a
    comparative column for this fiscal year) restates the FY segment figure.
    Re-running the deriver should chain the new Q4 value via supersedes_id,
    never mutate or duplicate the old row."""
    ticker = "TESTCO"
    _seed_clean_fy2025(conn)
    first = q4.derive_for_ticker(ticker, 2025, tmp_path, conn)
    assert first.derived_inserted == 1
    original = conn.execute(
        "SELECT id, value FROM segment_dimensions WHERE method_version = 'segment_q4_derive_v1'"
    ).fetchone()
    original_id, original_value = int(original["id"]), Decimal(str(original["value"]))

    # A later, still-within-comparative-window FY filing (2026 10-K carrying
    # a comparative column for FY2025) restates the FY segment figure.
    _insert_document(
        conn,
        doc_id=5,
        ticker=ticker,
        doc_type="fmp_10k_json",
        period_end="2026-12-31",
        fetched_at="2027-02-15",
    )
    p4b = _insert_period(
        conn, ticker=ticker, period_end="2025-12-31", fiscal_period_type="FY", source_doc_id=5
    )
    _insert_dim(conn, period_id=p4b, dim_name="North America", value="4700000000")

    second = q4.derive_for_ticker(ticker, 2025, tmp_path, conn)

    assert second.derived_inserted == 1
    assert second.superseded_count == 1
    new_row = conn.execute(
        "SELECT id, value, supersedes_id FROM segment_dimensions "
        "WHERE method_version = 'segment_q4_derive_v1' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert new_row["supersedes_id"] == original_id
    assert Decimal(str(new_row["value"])) != original_value
    # 4700M - (1000M+1200M+1000M) = 1500M
    assert Decimal(str(new_row["value"])) == Decimal("1500000000")
    # Old row is untouched, still present (never mutated/deleted).
    still_there = conn.execute(
        "SELECT value FROM segment_dimensions WHERE id = ?", (original_id,)
    ).fetchone()
    assert Decimal(str(still_there["value"])) == original_value


def test_recast_beyond_comparative_window_is_refused(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    """A 'recast' whose recasting document's own reporting year is further
    back than a 10-K's 2-prior-year comparative reach is out of algorithmic
    reach -- refused, not silently re-derived (§3.2 point 4)."""
    ticker = "TESTCO"
    _seed_clean_fy2025(conn)
    q4.derive_for_ticker(ticker, 2025, tmp_path, conn)
    original = conn.execute(
        "SELECT id, value FROM segment_dimensions WHERE method_version = 'segment_q4_derive_v1'"
    ).fetchone()
    original_id, original_value = int(original["id"]), Decimal(str(original["value"]))

    # A 10-K filed 4 fiscal years later (2029) claiming to carry a comparative
    # for FY2025 -- beyond any real 10-K's 2-prior-year comparative window.
    _insert_document(
        conn,
        doc_id=6,
        ticker=ticker,
        doc_type="fmp_10k_json",
        period_end="2029-12-31",
        fetched_at="2030-02-15",
    )
    p4c = _insert_period(
        conn, ticker=ticker, period_end="2025-12-31", fiscal_period_type="FY", source_doc_id=6
    )
    _insert_dim(conn, period_id=p4c, dim_name="North America", value="9999000000")

    result = q4.derive_for_ticker(ticker, 2025, tmp_path, conn)

    assert result.derived_inserted == 0
    assert result.not_computable_count == 1
    n_q4 = conn.execute(
        "SELECT COUNT(*) FROM segment_dimensions WHERE method_version = 'segment_q4_derive_v1'"
    ).fetchone()[0]
    assert n_q4 == 1  # no new row written
    unchanged = conn.execute(
        "SELECT value FROM segment_dimensions WHERE id = ?", (original_id,)
    ).fetchone()
    assert Decimal(str(unchanged["value"])) == original_value
    cov = conn.execute(
        "SELECT reason_code FROM segment_quarterly_coverage WHERE reason_code = 'recast_beyond_comparative_window'"
    ).fetchone()
    assert cov is not None


# ---------------------------------------------------------------------------
# Skip / degrade paths
# ---------------------------------------------------------------------------


def test_no_fy_data_skips_with_reason(tmp_path: Path, conn: sqlite3.Connection) -> None:
    result = q4.derive_for_ticker("NOPE", 2025, tmp_path, conn)
    assert result.skipped_reason == "no_fy_segment_data"


def test_no_quarterly_data_skips_with_reason(tmp_path: Path, conn: sqlite3.Connection) -> None:
    ticker = "TESTCO"
    _insert_document(
        conn,
        doc_id=4,
        ticker=ticker,
        doc_type="fmp_10k_json",
        period_end="2025-12-31",
        fetched_at="2026-02-15",
    )
    p4 = _insert_period(
        conn, ticker=ticker, period_end="2025-12-31", fiscal_period_type="FY", source_doc_id=4
    )
    _insert_dim(conn, period_id=p4, dim_name="North America", value="4400000000")

    result = q4.derive_for_ticker(ticker, 2025, tmp_path, conn)
    assert result.skipped_reason == "no_quarterly_segment_data"
