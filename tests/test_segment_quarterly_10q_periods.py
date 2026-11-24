"""Period-axis regression tests for compute.segment_quarterly_10q."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from compute.segment_quarterly_10q import extract_for_ticker

_SCHEMA = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    source_type TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
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
    method_version VARCHAR(32),
    CONSTRAINT uq_segment_periods_provenance UNIQUE
      (ticker, period_end, fiscal_period_type, source_doc_id)
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
    supersedes_id INTEGER
);
CREATE TABLE financial_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    period_end DATETIME NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    line_item TEXT NOT NULL,
    value NUMERIC NOT NULL
);
"""


def _seed_10q(
    conn: sqlite3.Connection,
    repo_root: Path,
    *,
    doc_id: int,
    ticker: str,
    year: int,
    quarter: str,
    payload: dict[str, object],
) -> None:
    out_dir = repo_root / "data" / "historical" / "fmp"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{ticker}_form_10q_{year}_{quarter}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    rel = str(path.relative_to(repo_root)).replace("\\", "/")
    conn.execute(
        "INSERT INTO documents "
        "(id, ticker, source_type, doc_type, file_path, sha256, fetched_at, "
        " fetch_status, raw_bytes_size) "
        "VALUES (?, ?, 'fmp', 'fmp_10q_json', ?, ?, ?, 'ok', 1)",
        (doc_id, ticker, rel, "0" * 64, datetime.now()),
    )
    conn.commit()


def test_prior_year_cumulative_stores_as_h1_cumulative_without_deriving(tmp_path: Path) -> None:
    """Prior-year 6M comparative column stores as H1/cumulative and does not trigger derivation."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT INTO tracked_companies (ticker, fiscal_year_end) VALUES (?, ?)",
            ("TESTCO", "12-31"),
        )
        conn.commit()

        q1_payload: dict[str, object] = {
            "symbol": "TESTCO",
            "Segment Note": [
                {
                    "Segment Reporting - Schedule of Segment Reporting Information "
                    "(Details) - USD ($) $ in Millions": ["3 Months Ended March 31, 2025"]
                },
                {"North America": ["\xa0"]},
                {"Total revenues": [1000.0]},
            ],
        }
        _seed_10q(
            conn, tmp_path, doc_id=1, ticker="TESTCO", year=2025, quarter="Q1", payload=q1_payload
        )
        extract_for_ticker("TESTCO", 2025, "Q1", tmp_path, conn)

        q2_payload: dict[str, object] = {
            "symbol": "TESTCO",
            "Segment Note": [
                {
                    "Segment Reporting - Schedule of Segment Reporting Information "
                    "(Details) - USD ($) $ in Millions": [
                        "6 Months Ended June 30, 2025",
                        "6 Months Ended June 30, 2024",
                    ]
                },
                {"North America": ["\xa0", "\xa0"]},
                {"Total revenues": [2200.0, 1900.0]},
            ],
        }
        _seed_10q(
            conn, tmp_path, doc_id=2, ticker="TESTCO", year=2025, quarter="Q2", payload=q2_payload
        )
        result = extract_for_ticker("TESTCO", 2025, "Q2", tmp_path, conn)

        assert result.derived_inserted == 1

        prior_rows = conn.execute(
            "SELECT sp.fiscal_period_type, sp.period_basis, sd.value, sd.disclosure_status "
            "FROM segment_dimensions sd JOIN segment_periods sp ON sp.id = sd.period_id "
            "WHERE sp.period_end LIKE '2024-06-30%'"
        ).fetchall()
        assert len(prior_rows) == 1
        assert prior_rows[0]["fiscal_period_type"] == "H1"
        assert prior_rows[0]["period_basis"] == "cumulative"
        assert Decimal(str(prior_rows[0]["value"])) == Decimal("1900000000")
        assert prior_rows[0]["disclosure_status"] == "reported"

        bad_prior = conn.execute(
            "SELECT COUNT(*) "
            "FROM segment_dimensions sd JOIN segment_periods sp ON sp.id = sd.period_id "
            "WHERE sp.period_end LIKE '2024-06-30%' "
            "AND (sp.fiscal_period_type = 'Q2' OR sd.disclosure_status = 'derived')"
        ).fetchone()[0]
        assert bad_prior == 0
    finally:
        conn.close()
