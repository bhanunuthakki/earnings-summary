"""Shared schema/fixtures for the competitive-tracking tests.

The minimal persist_manifest schema (kpi_definitions + kpi_facts + documents +
ingestion_runs + validation_issues) mirrors tests/test_ir_spreadsheet_ingest.py,
plus the `news` table for the S-1 watch path.
"""

from __future__ import annotations

import sqlite3

_KPI_SCHEMA = """
CREATE TABLE kpi_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, name TEXT NOT NULL, unit TEXT NOT NULL,
    primary_source TEXT NOT NULL, fallback_source TEXT, ir_url TEXT,
    threshold_tier TEXT, threshold_low FLOAT, threshold_high FLOAT, notes TEXT,
    definition_origin TEXT NOT NULL DEFAULT 'analyst',
    reporting_cadence TEXT NOT NULL DEFAULT 'quarterly',
    UNIQUE(ticker, name)
);
CREATE TABLE kpi_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, period_end TIMESTAMP NOT NULL, fiscal_period_type TEXT NOT NULL,
    kpi_definition_id INTEGER NOT NULL, value NUMERIC(24,6) NOT NULL, unit TEXT NOT NULL,
    source_doc_id INTEGER NOT NULL, confidence FLOAT NOT NULL DEFAULT 1.0,
    extracted_by TEXT, supersedes_id INTEGER, locator TEXT, source_excerpt TEXT
);
CREATE UNIQUE INDEX uq_kpi_facts_provenance
    ON kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, source_doc_id);
CREATE TABLE validation_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL, source_doc_id INTEGER, ticker TEXT,
    severity TEXT NOT NULL, rule TEXT NOT NULL, raw_value TEXT, expected TEXT,
    raised_at TIMESTAMP NOT NULL, resolved_at TIMESTAMP
);
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT, source_type TEXT, doc_type TEXT, period_end TIMESTAMP,
    file_path TEXT, sha256 TEXT, fetched_at TIMESTAMP, fetch_status TEXT,
    raw_bytes_size INTEGER, source_quality_tier TEXT
);
CREATE TABLE ingestion_runs (
    run_id TEXT PRIMARY KEY, started_at TIMESTAMP, ended_at TIMESTAMP,
    directive TEXT, ticker_scope TEXT, status TEXT, error_summary TEXT
);
"""

_NEWS_SCHEMA = """
CREATE TABLE news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, headline TEXT NOT NULL, url TEXT NOT NULL,
    published_at TEXT NOT NULL, snippet TEXT, source TEXT,
    source_feed TEXT NOT NULL DEFAULT 'fmp_stock_news', fetched_at TEXT NOT NULL,
    UNIQUE(ticker, url)
);
"""


def kpi_conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_KPI_SCHEMA)
    c.commit()
    return c


def news_conn_path(path: str) -> None:
    c = sqlite3.connect(path)
    c.executescript(_NEWS_SCHEMA)
    c.commit()
    c.close()
