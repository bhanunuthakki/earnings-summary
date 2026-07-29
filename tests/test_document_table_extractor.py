# pyright: reportPrivateUsage=false
"""Tests for src/document_table_extractor.py.

Exercises the orchestrator's dispatch logic, FMP-payload discovery
(by-FY and latest-on-disk), documents.id lookup, and aggregate
ExtractionOutcome assembly.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import document_table_extractor as dte
from models.runs import StageStatus
from pipeline.run_accounting import PipelineRunSuppressedError


def _full_schema(conn: sqlite3.Connection) -> None:
    """Schema combining lease_commitments + customer_concentrations +
    entity spine + documents + extractions. Mirrors a real DB enough
    that the orchestrator can resolve doc IDs."""
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR(16),
            source_type VARCHAR(32),
            doc_type VARCHAR(32),
            period_end DATE,
            file_path TEXT,
            sha256 VARCHAR(64),
            fetched_at DATETIME
        );
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
        CREATE TABLE customer_concentrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR(16) NOT NULL,
            fiscal_period VARCHAR(10) NOT NULL,
            fiscal_period_type VARCHAR(4) NOT NULL,
            customer_label VARCHAR(255) NOT NULL,
            customer_entity_id INTEGER,
            pct_of_revenue FLOAT NOT NULL,
            revenue_amount NUMERIC(24, 6),
            revenue_currency VARCHAR(3),
            source_doc_id INTEGER,
            source_excerpt TEXT,
            extracted_at DATETIME NOT NULL,
            UNIQUE(ticker, fiscal_period, customer_label)
        );
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind VARCHAR(32) NOT NULL,
            canonical_name VARCHAR(255) NOT NULL,
            display_name VARCHAR(255),
            external_ids TEXT, parent_entity_id INTEGER, meta_json TEXT,
            effective_from DATETIME, effective_to DATETIME,
            created_at DATETIME NOT NULL, last_observed_at DATETIME,
            UNIQUE(kind, canonical_name)
        );
        CREATE TABLE entity_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER NOT NULL,
            alias_text VARCHAR(255) NOT NULL,
            alias_kind VARCHAR(32) NOT NULL,
            first_observed_at DATETIME, last_observed_at DATETIME,
            observation_count INTEGER NOT NULL DEFAULT 1,
            confidence FLOAT NOT NULL DEFAULT 1.0,
            exemplar_source_doc_id INTEGER, exemplar_excerpt TEXT,
            UNIQUE(entity_id, alias_text)
        );
        CREATE TABLE concepts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind VARCHAR(32) NOT NULL,
            canonical_name VARCHAR(128) NOT NULL,
            unit_kind VARCHAR(32), taxonomy_xbrl_tag VARCHAR(128),
            generic_definition_md TEXT, computation_kind VARCHAR(32),
            computation_formula_md TEXT, created_at DATETIME NOT NULL,
            UNIQUE(canonical_name)
        );
        CREATE TABLE mapping_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind VARCHAR(48) NOT NULL,
            ticker VARCHAR(16), proposed_by VARCHAR(64) NOT NULL,
            payload_json TEXT NOT NULL, confidence FLOAT NOT NULL,
            source_doc_id INTEGER, source_excerpt TEXT,
            status VARCHAR(24) NOT NULL DEFAULT 'pending_review',
            applied_at DATETIME, applied_to_entity_id INTEGER,
            applied_to_concept_id INTEGER, decided_at DATETIME,
            decided_by VARCHAR(64), decision_notes TEXT,
            created_at DATETIME NOT NULL
        );
        CREATE TABLE extractions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_doc_id INTEGER NOT NULL,
            char_offset_start INTEGER, char_offset_end INTEGER,
            extraction_kind VARCHAR(48) NOT NULL,
            extractor_id VARCHAR(64) NOT NULL,
            extractor_version VARCHAR(16) NOT NULL,
            payload_json TEXT NOT NULL, confidence FLOAT NOT NULL DEFAULT 1.0,
            extracted_at DATETIME NOT NULL,
            superseded_by_extraction_id INTEGER, links_to_json TEXT
        );
        """
    )
    conn.commit()


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    """Build a tmp repo_root with data/portfolio.db + data/historical/fmp/."""
    (tmp_path / "data" / "historical" / "fmp").mkdir(parents=True)
    db_path = tmp_path / "data" / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    try:
        _full_schema(conn)
        # Seed one fmp_10k_json document row so doc_id lookup works.
        conn.execute(
            "INSERT INTO documents(ticker, source_type, doc_type, period_end, fetched_at) "
            "VALUES (?,?,?,?, datetime('now'))",
            ("GOOG", "fmp", "fmp_10k_json", "2024-12-31"),
        )
        conn.commit()
    finally:
        conn.close()
    return tmp_path


def _seed_fmp_json(repo_root: Path, ticker: str, year: int) -> Path:
    """Write a minimal FMP-shaped JSON with a real lease ladder section."""
    payload = {
        "symbol": ticker,
        "year": str(year),
        "Leases - Future Minimum Lease P": [
            {
                "Leases - Future Minimum Lease Payments (Details) - USD ($) $ in Millions": [
                    f"Dec. 31, {year}"
                ]
            },
            {"Operating Leases": ["\xa0"]},
            {f"{year + 1}": [1000]},
            {f"{year + 2}": [800]},
            {f"{year + 3}": [600]},
            {f"{year + 4}": [400]},
            {f"{year + 5}": [200]},
            {"Thereafter": [500]},
            {"Total future lease payments": [3500]},
            {"Less imputed interest": [-300]},
            {"Total lease liability balance": [3200]},
        ],
    }
    p = repo_root / "data" / "historical" / "fmp" / f"{ticker}_form_10k_{year}.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------


def test_registered_table_kinds_contains_mvp_extractors() -> None:
    kinds = dte.registered_table_kinds()
    assert "customer_concentration" in kinds
    assert "lease_commitments" in kinds


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def test_extract_for_ticker_dispatches_to_lease_commitments(
    repo_root: Path,
) -> None:
    _seed_fmp_json(repo_root, "GOOG", 2024)
    outcomes = dte.extract_for_ticker(
        ticker="GOOG",
        fiscal_year=2024,
        table_kinds=["lease_commitments"],
        repo_root=repo_root,
    )
    assert len(outcomes) == 1
    lease_out = outcomes[0]
    assert lease_out.table_kind == "lease_commitments_ladder"
    assert lease_out.status == "ok"
    assert lease_out.n_rows_inserted == 9  # only operating leases in the seed


def test_extract_for_ticker_picks_latest_year_when_unspecified(
    repo_root: Path,
) -> None:
    _seed_fmp_json(repo_root, "GOOG", 2022)
    _seed_fmp_json(repo_root, "GOOG", 2023)
    _seed_fmp_json(repo_root, "GOOG", 2024)
    outcomes = dte.extract_for_ticker(
        ticker="GOOG",
        fiscal_year=None,  # let orchestrator pick latest
        table_kinds=["lease_commitments"],
        repo_root=repo_root,
    )
    assert outcomes[0].fiscal_year == 2024


def test_extract_for_ticker_returns_no_data_when_no_fmp_cached(
    repo_root: Path,
) -> None:
    # No JSON seeded.
    outcomes = dte.extract_for_ticker(
        ticker="UNKNOWN",
        fiscal_year=2024,
        table_kinds=["lease_commitments"],
        repo_root=repo_root,
    )
    assert len(outcomes) == 1
    assert outcomes[0].status == "no_data"
    assert "no FMP" in outcomes[0].notes


def test_extract_for_ticker_returns_no_data_when_requested_fy_missing(
    repo_root: Path,
) -> None:
    _seed_fmp_json(repo_root, "GOOG", 2024)
    outcomes = dte.extract_for_ticker(
        ticker="GOOG",
        fiscal_year=2099,  # not on disk
        table_kinds=["lease_commitments"],
        repo_root=repo_root,
    )
    assert outcomes[0].status == "no_data"


def test_extract_for_ticker_runs_all_registered_by_default(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Mock the LLM so the customer_concentration extractor doesn't make
    # a real call. The lease extractor is deterministic.
    def empty_llm(_prompt: str, **_kwargs: object) -> str:
        return "[]"

    monkeypatch.setattr(
        "table_extractors.customer_concentration.call_llm",
        empty_llm,
    )
    _seed_fmp_json(repo_root, "GOOG", 2024)
    outcomes = dte.extract_for_ticker(
        ticker="GOOG",
        fiscal_year=2024,
        table_kinds=None,
        repo_root=repo_root,
    )
    # The default sweep runs the two narrow bespoke extractors. The capture-all
    # walker is opt-in (it can mint thousands of facts), so it does NOT ride the
    # default run even though it's registered.
    table_kinds = {o.table_kind for o in outcomes}
    assert table_kinds == {"customer_concentration", "lease_commitments_ladder"}


def test_capture_all_is_registered_but_opt_in(repo_root: Path) -> None:
    """xbrl_capture_all is a valid --table-kind choice but excluded from the
    default (table_kinds=None) sweep — only runs when named explicitly."""
    assert "xbrl_capture_all" in dte.registered_table_kinds()
    assert dte._REGISTRY["xbrl_capture_all"].default is False  # pyright: ignore[reportPrivateUsage]
    assert all(
        e.default
        for k, e in dte._REGISTRY.items()
        if k != "xbrl_capture_all"  # pyright: ignore[reportPrivateUsage]
    )


def test_extract_for_ticker_rejects_unknown_kind(repo_root: Path) -> None:
    with pytest.raises(ValueError, match="unknown table_kind"):
        dte.extract_for_ticker(
            ticker="GOOG",
            fiscal_year=2024,
            table_kinds=["nonexistent_kind"],
            repo_root=repo_root,
        )


def test_extract_for_ticker_propagates_accounting_suppression(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_fmp_json(repo_root, "GOOG", 2024)
    suppressed = PipelineRunSuppressedError(
        pipeline_key="pipeline_same",
        attempt_id="attempt_live",
        status=StageStatus.IN_PROGRESS,
    )

    def suppress(**_: object):
        raise suppressed

    monkeypatch.setattr(dte._REGISTRY["lease_commitments"], "extract_fn", suppress)
    with pytest.raises(PipelineRunSuppressedError) as exc_info:
        dte.extract_for_ticker(
            ticker="GOOG",
            fiscal_year=2024,
            table_kinds=["lease_commitments"],
            repo_root=repo_root,
        )
    assert exc_info.value is suppressed


def test_extract_for_ticker_resolves_doc_id_from_documents_table(
    repo_root: Path,
) -> None:
    _seed_fmp_json(repo_root, "GOOG", 2024)
    outcomes = dte.extract_for_ticker(
        ticker="GOOG",
        fiscal_year=2024,
        table_kinds=["lease_commitments"],
        repo_root=repo_root,
    )
    assert outcomes[0].n_extractions_logged == 9  # all rows logged with the seeded doc_id

    # Verify the extractions row carries the right source_doc_id (the
    # seeded fmp_10k_json document had id=1).
    conn = sqlite3.connect(str(repo_root / "data" / "portfolio.db"))
    try:
        row = conn.execute(
            "SELECT source_doc_id FROM extractions WHERE extractor_id='lease_commitments_v1' LIMIT 1"
        ).fetchone()
        assert row is not None
        assert int(row[0]) == 1
    finally:
        conn.close()


def test_extract_for_ticker_handles_missing_documents_table_gracefully(
    tmp_path: Path,
) -> None:
    """A fresh DB with no documents row: orchestrator still extracts, just
    doesn't log to extractions (which is fine — typed rows still land)."""
    (tmp_path / "data" / "historical" / "fmp").mkdir(parents=True)
    db_path = tmp_path / "data" / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    try:
        # Only the lease_commitments table, NO documents table.
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
            """
        )
        conn.commit()
    finally:
        conn.close()
    _seed_fmp_json(tmp_path, "GOOG", 2024)
    outcomes = dte.extract_for_ticker(
        ticker="GOOG",
        fiscal_year=2024,
        table_kinds=["lease_commitments"],
        repo_root=tmp_path,
    )
    # Typed rows landed even though doc_id resolution failed silently.
    assert outcomes[0].n_rows_inserted == 9
    assert outcomes[0].n_extractions_logged == 0
