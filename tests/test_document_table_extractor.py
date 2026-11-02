"""Tests for src/document_table_extractor.py.

Exercises the orchestrator's dispatch logic, FMP-payload discovery
(by-FY and latest-on-disk), documents.id lookup, and aggregate
ExtractionOutcome assembly.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

import document_table_extractor as dte
from models.runs import StageStatus
from pipeline.fmp_doc_index import register_fmp_snapshot
from pipeline.run_accounting import PipelineRunSuppressedError
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


@pytest.fixture()
def repo_root(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    """Build the current schema with a retained synthetic FMP source."""
    (tmp_path / "data" / "historical" / "fmp").mkdir(parents=True)
    db_path = migrated_db(tmp_path / "data" / "portfolio.db")
    source = _seed_fmp_json(tmp_path, "GOOG", 2024)
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    try:
        conn.execute("BEGIN")
        register_fmp_snapshot(
            conn,
            ticker="GOOG",
            source_filename=source.name,
            content=source.read_bytes(),
            project_root=tmp_path,
            captured_at=datetime(2026, 1, 1, tzinfo=UTC),
            period_end=datetime(2024, 12, 31),
        )
        conn.commit()
    finally:
        conn.close()
    # Cache discovery remains empty until a test explicitly seeds its input.
    source.unlink()
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
    def empty_llm(_prompt: str, **_kwargs: object) -> list[object]:
        return []

    monkeypatch.setattr(
        "table_extractors.customer_concentration.call_llm_structured",
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
    """Capture-all is registered and selected only by an explicit request."""
    assert "xbrl_capture_all" in dte.registered_table_kinds()
    default = dte.extract_for_ticker(ticker="GOOG", fiscal_year=2024, repo_root=repo_root)
    assert [(outcome.table_kind, outcome.status) for outcome in default] == [
        ("customer_concentration", "no_data"),
        ("lease_commitments_ladder", "no_data"),
    ]
    explicit = dte.extract_for_ticker(
        ticker="GOOG",
        fiscal_year=2024,
        table_kinds=["xbrl_capture_all"],
        repo_root=repo_root,
    )
    assert [(outcome.table_kind, outcome.status) for outcome in explicit] == [
        ("xbrl_capture_all", "no_data")
    ]


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

    def suppress(**_: object) -> None:
        raise suppressed

    registry: Mapping[str, object] = getattr(dte, "_REGISTRY")
    monkeypatch.setattr(registry["lease_commitments"], "extract_fn", suppress)
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
