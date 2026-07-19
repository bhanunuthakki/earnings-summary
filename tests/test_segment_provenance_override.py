"""End-to-end segment-path provenance override — the GOOG Q4'25 worked example.

Proves the durable override (a record-level ``segment`` replace seeded from the
SEC 8-K) makes the company-published figures win across every consumer of the raw
FMP segment cache, even when a re-fetch has re-introduced FMP's contaminated
record:

  * the DB-ingest gate (``compute.segments.extract_segment_facts``) — the bad
    record would normally be DROPPED (sum 1.35x revenue); with the override it is
    corrected first, reconciles, and lands the 8-K figures (Google Cloud $17.664B);
  * the segment-cache audit (``pipeline.segment_cache_audit.audit_ticker_cache``)
    — the overridden quarter reconciles and is no longer flagged;
  * the direct-JSON DCF-reader path (``compute.segment_cache.apply_overrides``) —
    returns the corrected ``data`` dict;
  * and a Q4 override never bleeds into the same-dated FY annual rollup.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from compute import segment_cache
from compute.segments import extract_segment_facts
from pipeline.segment_cache_audit import audit_ticker_cache
from provenance import overrides
from provenance.overrides import OverrideAction

# GOOG Q4'25 product segments per the SEC 8-K exhibit 99.1 (leaf sum $113,828M).
_GOOG_8K: dict[str, object] = {
    "Google Search & Other": 63073000000,
    "YouTube Advertising Revenue": 11383000000,
    "Google Network": 7828000000,
    "Subscriptions, Platforms, And Devices Revenue": 13578000000,
    "Google Cloud": 17664000000,
    "Other Bets": 370000000,
    "Other Segments": -68000000,
}

# A re-fetched CONTAMINATED Q4 record: inflated Google Cloud, the spurious
# "Google Inc." bucket carrying the prior-FY annual figure, Subscriptions missing.
# Sum ~ $152.7B = 1.35x the $113.5B reported revenue -> over the ingest gate's cap.
_GOOG_BAD: dict[str, object] = {
    "Google Search & Other": 63073000000,
    "YouTube Advertising Revenue": 11383000000,
    "Google Network": 7828000000,
    "Google Cloud": 20941000000,
    "Google Inc.": 48030000000,
    "Other Bets": 1537000000,
    "Other Segments": -127000000,
}

_REVENUE = 113_500_000_000

_OVERRIDES_DDL = """
CREATE TABLE fact_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu',
    ticker TEXT NOT NULL,
    period_end TEXT NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    fact_kind TEXT NOT NULL,
    fact_key TEXT NOT NULL,
    action TEXT NOT NULL,
    value NUMERIC,
    unit TEXT,
    value_json TEXT,
    source_doc_type TEXT NOT NULL,
    source_accession TEXT,
    source_exhibit TEXT,
    source_url TEXT,
    source_excerpt TEXT,
    source_doc_id INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    confidence REAL,
    rationale TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    retired_at TEXT,
    locator TEXT,
    CHECK (fact_kind IN ('financial_fact', 'segment', 'kpi')),
    CHECK (action IN ('replace', 'drop', 'qualify')),
    CHECK (status IN ('active', 'retired'))
);
CREATE UNIQUE INDEX uq_fact_overrides_active ON fact_overrides
    (user_id, ticker, period_end, fiscal_period_type, fact_kind, fact_key)
    WHERE status = 'active';
"""

_SCHEMA_DDL = (
    """
    CREATE TABLE documents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        source_type TEXT NOT NULL,
        doc_type TEXT NOT NULL,
        file_path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        fetched_at TIMESTAMP NOT NULL,
        fetch_status TEXT NOT NULL,
        raw_bytes_size INTEGER NOT NULL,
        source_quality_tier TEXT NOT NULL DEFAULT 'fmp_normalized'
    );
    CREATE TABLE financial_facts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        period_end TIMESTAMP NOT NULL,
        fiscal_period_type TEXT NOT NULL,
        line_item TEXT NOT NULL,
        value NUMERIC(24, 6) NOT NULL,
        unit TEXT NOT NULL DEFAULT 'actual',
        source_doc_id INTEGER
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
        CONSTRAINT uq_segment_periods_provenance UNIQUE
          (ticker, period_end, fiscal_period_type, source_doc_id)
    );
    CREATE TABLE segment_dimensions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        period_id INTEGER NOT NULL REFERENCES segment_periods(id),
        dim_type VARCHAR(16) NOT NULL,
        dim_name VARCHAR(128) NOT NULL,
        value NUMERIC(20, 4) NOT NULL,
        metric VARCHAR(32) NOT NULL
    );
    """
    + _OVERRIDES_DDL
)


def _bad_quarterly_records() -> list[dict[str, object]]:
    return [
        {
            "symbol": "GOOG",
            "fiscalYear": 2025,
            "period": "Q4",
            "reportedCurrency": "USD",
            "date": "2025-12-31",
            "data": dict(_GOOG_BAD),
        }
    ]


def _write_cache(project_root: Path, filename: str, records: list[dict[str, object]]) -> None:
    fmp = project_root / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True, exist_ok=True)
    (fmp / filename).write_text(json.dumps(records), encoding="utf-8")


def _seed_db(conn: sqlite3.Connection) -> int:
    conn.executescript(_SCHEMA_DDL)
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256, "
        "fetched_at, fetch_status, raw_bytes_size) VALUES "
        "(1, 'GOOG', 'fmp', 'fmp_segment_product', "
        "'data/historical/fmp/GOOG_product_segments_quarterly.json', ?, ?, 'ok', 1)",
        ("0" * 64, datetime(2026, 6, 14)),
    )
    conn.execute(
        "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, line_item, value) "
        "VALUES ('GOOG', ?, 'Q4', 'revenue', ?)",
        (datetime(2025, 12, 31), _REVENUE),
    )
    conn.commit()
    return 1


def _seed_override(conn: sqlite3.Connection) -> None:
    overrides.record_override(
        conn,
        ticker="GOOG",
        period_end="2025-12-31",
        fiscal_period_type="Q4",
        fact_kind=overrides.SEGMENT,
        fact_key=overrides.segment_record_key("product"),
        action=OverrideAction.REPLACE,
        value_json=dict(_GOOG_8K),
        source_doc_type="sec_8k",
        source_accession="0001652044-26-000012",
        source_exhibit="googexhibit991q42025.htm",
        rationale="8-K exhibit 99.1 supersedes contaminated FMP segmentation",
        created_by="test",
    )
    conn.commit()


@pytest.fixture
def env(tmp_path: Path) -> tuple[sqlite3.Connection, Path]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _seed_db(conn)
    _write_cache(tmp_path, "GOOG_product_segments_quarterly.json", _bad_quarterly_records())
    return conn, tmp_path


def _gcp_in_junction(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT sd.value FROM segment_dimensions sd "
        "JOIN segment_periods sp ON sp.id = sd.period_id "
        "WHERE sp.ticker = 'GOOG' AND sd.dim_name = 'Google Cloud'"
    ).fetchone()
    return None if row is None else int(row["value"])


def test_extract_drops_contaminated_without_override(env: tuple[sqlite3.Connection, Path]) -> None:
    conn, project_root = env
    inserted = extract_segment_facts(conn, 1, project_root)
    # No override: the over-cap record is dropped by the reconciliation gate.
    assert inserted == 0
    assert conn.execute("SELECT COUNT(*) FROM segment_dimensions").fetchone()[0] == 0


def test_extract_applies_override_landing_8k_figures(
    env: tuple[sqlite3.Connection, Path],
) -> None:
    conn, project_root = env
    _seed_override(conn)
    inserted = extract_segment_facts(conn, 1, project_root)
    # With the override the corrected record reconciles and all 7 segments land.
    assert inserted == 7
    assert _gcp_in_junction(conn) == 17664000000
    # The contaminated bucket never reached the DB; Subscriptions did.
    names = {
        r["dim_name"] for r in conn.execute("SELECT dim_name FROM segment_dimensions").fetchall()
    }
    assert "Google Inc." not in names
    assert "Subscriptions, Platforms, And Devices Revenue" in names


def test_audit_flags_without_override_skips_with(env: tuple[sqlite3.Connection, Path]) -> None:
    conn, project_root = env
    data_dir = str(project_root / "data" / "historical" / "fmp")
    _write_cache(
        project_root,
        "GOOG_income_statement_quarterly.json",
        [{"date": "2025-12-31", "period": "Q4", "revenue": _REVENUE}],
    )
    # Without a conn (no overrides consulted) the contaminated quarter is flagged.
    flags = audit_ticker_cache(data_dir, "GOOG")
    assert any(f.period_end[:10] == "2025-12-31" for f in flags)
    # With the override applied, it reconciles and is no longer flagged.
    _seed_override(conn)
    assert audit_ticker_cache(data_dir, "GOOG", conn=conn) == []


def test_dcf_reader_path_via_db_path(tmp_path: Path) -> None:
    # A file-backed DB exercises segment_cache.apply_overrides' best-effort DB open
    # (the path the direct-JSON DCF readers take).
    db = tmp_path / "portfolio.db"
    fconn = sqlite3.connect(str(db))
    fconn.row_factory = sqlite3.Row
    fconn.executescript(_OVERRIDES_DDL)
    _seed_override(fconn)
    fconn.close()

    out = segment_cache.apply_overrides(
        _bad_quarterly_records(), ticker="GOOG", dim_type="product", db_path=str(db)
    )
    data = out[0]["data"]
    assert isinstance(data, dict)
    assert data["Google Cloud"] == 17664000000
    assert "Google Inc." not in data


def test_no_db_is_passthrough(tmp_path: Path) -> None:
    # No DB file at the path -> records returned unchanged (raw FMP), never raising.
    out = segment_cache.apply_overrides(
        _bad_quarterly_records(),
        ticker="GOOG",
        dim_type="product",
        db_path=str(tmp_path / "absent.db"),
    )
    data = out[0]["data"]
    assert isinstance(data, dict)
    assert data["Google Cloud"] == 20941000000  # unchanged


def test_q4_override_does_not_touch_fy_record(env: tuple[sqlite3.Connection, Path]) -> None:
    conn, _ = env
    _seed_override(conn)  # a Q4 override at 2025-12-31
    fy_record: dict[str, object] = {
        "symbol": "GOOG",
        "fiscalYear": 2025,
        "period": "FY",
        "date": "2025-12-31",  # same date as the Q4 override
        "data": {"Google Cloud": 58705000000, "Google Inc.": 48030000000},
    }
    out = segment_cache.apply_overrides([fy_record], ticker="GOOG", dim_type="product", conn=conn)
    data = out[0]["data"]
    assert isinstance(data, dict)
    # FY annual rollup is untouched by the Q4 override (period discriminates).
    assert data["Google Cloud"] == 58705000000
    assert "Google Inc." in data
