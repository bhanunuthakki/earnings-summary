"""Tests for src/pipeline/sec_xbrl.py — period-span resolution + accession upsert + tag mapping."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from models.facts import FiscalPeriodType
from pipeline.sec_xbrl import (
    CIK_MAP,
    _AccessionRecord,
    _enumerate_accessions,
    _period_span_months,
    _resolve_fiscal_period_type,
    insert_facts_from_companyfacts,
    upsert_accession_documents,
)


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            source_type TEXT NOT NULL,
            doc_type TEXT NOT NULL,
            period_start TIMESTAMP,
            period_end TIMESTAMP,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            fetched_at TIMESTAMP NOT NULL,
            fetch_status TEXT NOT NULL,
            http_code INTEGER,
            raw_bytes_size INTEGER NOT NULL,
            source_url TEXT,
            parent_document_id INTEGER
        );
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_end TIMESTAMP NOT NULL,
            fiscal_period_type TEXT NOT NULL,
            line_item TEXT NOT NULL,
            value NUMERIC(24,6) NOT NULL,
            currency TEXT,
            unit TEXT NOT NULL,
            source_doc_id INTEGER NOT NULL,
            confidence REAL DEFAULT 1.0
        );
        """
    )
    conn.commit()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _create_schema(c)
    return c


def test_cik_map_covers_all_tracked_tickers() -> None:
    """Every portfolio + watchlist + ETF ticker should have a CIK except FLKR (ETF)."""
    expected = {
        "ABNB", "AMAT", "AMZN", "ASML", "BHP", "BKNG", "BN", "CNQ", "FCX", "FNV",
        "GOOG", "HDB", "JPM", "LLY", "LMND", "MELI", "META", "MU", "NOW", "NU",
        "NVO", "RBRK", "RIO", "SOFI", "TOL", "TPL", "TSM", "VALE", "VEEV",
        "WIX", "WPM", "WY",
    }
    assert expected == set(CIK_MAP)


def test_period_span_months_handles_quarterly() -> None:
    """Q3 fact: start=2025-07-01, end=2025-09-30 -> ~3 months."""
    assert _period_span_months("2025-07-01", "2025-09-30") == 3


def test_period_span_months_handles_annual() -> None:
    assert _period_span_months("2024-01-01", "2024-12-31") == 12


def test_period_span_months_handles_ytd_9month() -> None:
    """9-month YTD: start=2025-01-01, end=2025-09-30 -> ~9 months."""
    assert _period_span_months("2025-01-01", "2025-09-30") == 9


def test_resolve_period_skips_9month_aggregation() -> None:
    """9M YTD value at end=Sep 30 -> None (would conflict with Q3 standalone)."""
    fpt = _resolve_fiscal_period_type(fp="Q3", start_date="2025-01-01", end_date="2025-09-30")
    assert fpt is None


def test_resolve_period_returns_q3_for_3month_at_sep30() -> None:
    fpt = _resolve_fiscal_period_type(fp="Q3", start_date="2025-07-01", end_date="2025-09-30")
    assert fpt == FiscalPeriodType.Q3


def test_resolve_period_returns_fy_for_12month() -> None:
    fpt = _resolve_fiscal_period_type(fp="FY", start_date="2024-01-01", end_date="2024-12-31")
    assert fpt == FiscalPeriodType.FY


def test_resolve_period_returns_q4_for_balance_sheet_at_dec31() -> None:
    """Balance-sheet items have only `end`; treat Dec 31 as FY snapshot."""
    fpt = _resolve_fiscal_period_type(fp=None, start_date=None, end_date="2024-12-31")
    assert fpt == FiscalPeriodType.FY


def test_enumerate_accessions_dedupes() -> None:
    """Same accession appearing in multiple tag entries gets a single record."""
    payload = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {"accn": "0001-22-001", "form": "10-K", "filed": "2025-02-01", "fy": 2024, "fp": "FY",
                             "start": "2024-01-01", "end": "2024-12-31", "val": 100},
                            {"accn": "0001-22-001", "form": "10-K", "filed": "2025-02-01", "fy": 2024, "fp": "FY",
                             "start": "2024-01-01", "end": "2024-12-31", "val": 100},
                        ]
                    }
                }
            }
        }
    }
    out = _enumerate_accessions(payload)
    assert len(out) == 1
    assert "0001-22-001" in out


def test_upsert_accession_documents_idempotent(conn: sqlite3.Connection) -> None:
    """Re-running upsert with the same accessions inserts no new documents."""
    accessions = {
        "0001-22-001": _AccessionRecord(
            accession="0001-22-001", form="10-K", filed="2025-02-01", fy=2024, fp="FY",
        ),
    }
    first = upsert_accession_documents(conn, ticker="X", accessions=accessions, project_root=Path("/tmp"))
    second = upsert_accession_documents(conn, ticker="X", accessions=accessions, project_root=Path("/tmp"))
    assert first == second
    n = conn.execute("SELECT COUNT(*) FROM documents WHERE source_type='sec_xbrl'").fetchone()[0]
    assert n == 1


def test_insert_facts_skips_ytd_aggregations(conn: sqlite3.Connection) -> None:
    """Mixed payload: Q3 standalone (3 month) + 9M YTD; only Q3 gets inserted."""
    accessions = {
        "0001-22-001": _AccessionRecord(
            accession="0001-22-001", form="10-Q", filed="2025-10-30", fy=2025, fp="Q3",
        ),
    }
    accn_to_doc = upsert_accession_documents(
        conn, ticker="X", accessions=accessions, project_root=Path("/tmp")
    )
    payload = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            # Q3 standalone (3 months)
                            {"accn": "0001-22-001", "form": "10-Q", "filed": "2025-10-30",
                             "fy": 2025, "fp": "Q3",
                             "start": "2025-07-01", "end": "2025-09-30", "val": 4_000_000_000},
                            # 9-month YTD (skipped)
                            {"accn": "0001-22-001", "form": "10-Q", "filed": "2025-10-30",
                             "fy": 2025, "fp": "Q3",
                             "start": "2025-01-01", "end": "2025-09-30", "val": 11_000_000_000},
                        ]
                    }
                }
            }
        }
    }
    inserted = insert_facts_from_companyfacts(
        conn, ticker="X", payload=payload, accession_to_doc_id=accn_to_doc
    )
    assert inserted == 1
    rows = conn.execute("SELECT value, fiscal_period_type FROM financial_facts WHERE ticker='X'").fetchall()
    assert len(rows) == 1
    assert int(dict(rows[0])["value"]) == 4_000_000_000
    assert dict(rows[0])["fiscal_period_type"] == "Q3"
