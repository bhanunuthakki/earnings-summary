"""Plan 7.7: no-data-shape-change guard for the /api/v3 -> /stable migration.

The migration retired the v3 fetchers but must NOT change the on-disk JSON shape
or file naming. The live stable cacher (execution/save_fmp_data.py) writes
SUFFIXED `{TICKER}_income_statement_annual.json` with stable field names
(date / period / fiscalYear / revenue / ...). This pins that a persisted
stable-shaped annual statement still extracts to the expected financial_facts —
so a future change to the on-disk shape or the extractor is caught.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from compute.income_statement import extract_income_statement_facts

# Two annual periods, stable field names + the SUFFIXED `_annual` file naming —
# exactly the shape execution/save_fmp_data.py persists.
_STABLE_ANNUAL: list[dict[str, object]] = [
    {
        "date": "2025-12-31",
        "symbol": "GOOG",
        "reportedCurrency": "USD",
        "period": "FY",
        "fiscalYear": "2025",
        "revenue": 402_963_000_000,
        "netIncome": 132_170_000_000,
        "operatingIncome": 129_166_000_000,
        "ebit": 129_166_000_000,
    },
    {
        "date": "2024-12-31",
        "symbol": "GOOG",
        "reportedCurrency": "USD",
        "period": "FY",
        "fiscalYear": "2024",
        "revenue": 350_018_000_000,
        "netIncome": 100_118_000_000,
        "operatingIncome": 112_390_000_000,
        "ebit": 112_390_000_000,
    },
]


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            source_type TEXT NOT NULL,
            doc_type TEXT NOT NULL,
            period_end TIMESTAMP,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            fetched_at TIMESTAMP NOT NULL,
            fetch_status TEXT NOT NULL,
            raw_bytes_size INTEGER NOT NULL
        );
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_end TIMESTAMP NOT NULL,
            fiscal_period_type TEXT NOT NULL,
            line_item TEXT NOT NULL,
            value NUMERIC(24, 6) NOT NULL,
            currency TEXT,
            unit TEXT NOT NULL,
            source_doc_id INTEGER NOT NULL,
            confidence REAL NOT NULL DEFAULT 1.0
        );
        CREATE UNIQUE INDEX uq_financial_facts_provenance
        ON financial_facts (ticker, period_end, fiscal_period_type, line_item, source_doc_id);
        """
    )
    conn.commit()
    return conn


def test_persisted_stable_annual_extracts_expected_facts(tmp_path: Path) -> None:
    conn = _make_conn()
    try:
        fmp_dir = tmp_path / "data" / "historical" / "fmp"
        fmp_dir.mkdir(parents=True)
        # SUFFIXED file naming is part of the on-disk contract this guard pins.
        rel = "data/historical/fmp/GOOG_income_statement_annual.json"
        (tmp_path / rel).write_text(json.dumps(_STABLE_ANNUAL), encoding="utf-8")

        conn.execute(
            "INSERT INTO documents "
            "(ticker, source_type, doc_type, file_path, sha256, fetched_at, "
            " fetch_status, raw_bytes_size) "
            "VALUES ('GOOG', 'fmp', 'fmp_income_statement', ?, 'deadbeef', ?, 'ok', 1000)",
            (rel, datetime.now()),
        )
        conn.commit()
        doc_id = conn.execute("SELECT id FROM documents").fetchone()["id"]

        inserted = extract_income_statement_facts(conn, doc_id, project_root=tmp_path)
        assert inserted > 0

        rows = conn.execute(
            "SELECT period_end, fiscal_period_type, line_item, value, currency "
            "FROM financial_facts WHERE ticker = 'GOOG' AND line_item = 'revenue' "
            "ORDER BY period_end"
        ).fetchall()
        revenue_by_period = {str(r["period_end"])[:10]: r for r in rows}

        assert set(revenue_by_period) == {"2024-12-31", "2025-12-31"}
        assert int(revenue_by_period["2025-12-31"]["value"]) == 402_963_000_000
        assert int(revenue_by_period["2024-12-31"]["value"]) == 350_018_000_000
        assert revenue_by_period["2025-12-31"]["fiscal_period_type"] == "FY"
        assert revenue_by_period["2025-12-31"]["currency"] == "USD"

        # Net income for the latest period is also pinned (stable field name).
        ni_rows = conn.execute(
            "SELECT period_end, value FROM financial_facts "
            "WHERE ticker = 'GOOG' AND line_item = 'net_income' ORDER BY period_end"
        ).fetchall()
        ni_by_period = {str(r["period_end"])[:10]: int(r["value"]) for r in ni_rows}
        assert ni_by_period.get("2025-12-31") == 132_170_000_000
    finally:
        conn.close()
