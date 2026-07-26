"""Regression tests for additive data-integrity foundation behavior."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from dcf.persist import DcfRunRow, upsert
from dcf.provenance import DcfInputProvenance
from pipeline.run_accounting import make_pipeline_key, start_run
from pipeline.validation_issue_store import record_issue
from schema_compat import SchemaRevisionMismatch, require_current_for_write
from timeseries.loaders import load_segment_junction_series


def test_pipeline_key_is_stable_across_attempts_and_scope_order() -> None:
    assert make_pipeline_key("refresh", ["meta", "GOOG"]) == make_pipeline_key(
        "refresh", ["GOOG", "META"]
    )


def test_start_run_writes_separate_logical_run_and_attempt() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE ingestion_runs (run_id TEXT, attempt_id TEXT, pipeline_key TEXT, "
        "started_at TEXT, ended_at TEXT, directive TEXT, ticker_scope TEXT, status TEXT, error_summary TEXT)"
    )
    conn.execute(
        "CREATE TABLE pipeline_runs (pipeline_key TEXT PRIMARY KEY, directive TEXT, ticker_scope TEXT, first_started_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE pipeline_attempts (attempt_id TEXT PRIMARY KEY, pipeline_key TEXT, started_at TEXT, ended_at TEXT, status TEXT, error_summary TEXT)"
    )
    run_a = start_run(conn, "refresh", ["META", "GOOG"])
    run_b = start_run(conn, "refresh", ["GOOG", "META"])
    assert run_a != run_b
    assert conn.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM pipeline_attempts").fetchone()[0] == 2


def test_validation_issue_fingerprint_advances_lifecycle_not_row_count() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE validation_issues (id INTEGER PRIMARY KEY, run_id TEXT, source_doc_id INTEGER, "
        "ticker TEXT, severity TEXT, rule TEXT, raw_value TEXT, expected TEXT, raised_at TEXT, "
        "resolved_at TEXT, fingerprint TEXT, first_seen_at TEXT, last_seen_at TEXT, occurrence_count INTEGER)"
    )
    first = record_issue(
        conn,
        run_id="a",
        source_doc_id=7,
        ticker="goog",
        severity="warn",
        rule="range",
        raw_value="x=9",
        expected="x<2",
    )
    second = record_issue(
        conn,
        run_id="b",
        source_doc_id=7,
        ticker="GOOG",
        severity="warn",
        rule="range",
        raw_value="x=9",
        expected="x<2",
    )
    row = conn.execute("SELECT run_id, occurrence_count FROM validation_issues").fetchone()
    assert first == second
    assert row == ("b", 2)


def test_versioned_write_refuses_revision_mismatch() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE alembic_version (version_num TEXT)")
    conn.execute("INSERT INTO alembic_version VALUES ('old_revision')")
    try:
        require_current_for_write(conn)
    except SchemaRevisionMismatch as exc:
        assert "alembic upgrade head" in str(exc)
    else:  # pragma: no cover - test must fail if writes are permitted
        raise AssertionError("mismatched schema was accepted")


def test_segment_as_of_uses_only_pre_cutoff_document(tmp_path: Path) -> None:
    db = tmp_path / "segments.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE documents (id INTEGER PRIMARY KEY, source_quality_tier TEXT, fetched_at TEXT);
        CREATE TABLE segment_periods (id INTEGER PRIMARY KEY, ticker TEXT, period_end TEXT,
            fiscal_period_type TEXT, source_doc_id INTEGER, unit TEXT);
        CREATE TABLE segment_dimensions (id INTEGER PRIMARY KEY, period_id INTEGER, dim_type TEXT,
            dim_name TEXT, metric TEXT, value REAL);
        INSERT INTO documents VALUES (1, 'sec_official', '2026-01-01');
        INSERT INTO documents VALUES (2, 'sec_official', '2026-02-01');
        INSERT INTO segment_periods VALUES (1, 'GOOG', '2025-12-31', 'Q4', 1, 'USD');
        INSERT INTO segment_periods VALUES (2, 'GOOG', '2025-12-31', 'Q4', 2, 'USD');
        INSERT INTO segment_dimensions VALUES (1, 1, 'product', 'Cloud', 'revenue', 10);
        INSERT INTO segment_dimensions VALUES (2, 2, 'product', 'Cloud', 'revenue', 20);
        """
    )
    conn.commit()
    conn.close()
    historical = load_segment_junction_series(
        "GOOG", [("product", "Cloud")], "revenue", db_path=db, as_of_date="2026-01-15"
    )
    current = load_segment_junction_series("GOOG", [("product", "Cloud")], "revenue", db_path=db)
    assert [item.value for item in historical] == [10.0]
    assert [item.value for item in current] == [20.0]


def test_dcf_provenance_refuses_a_pre_migration_schema() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE dcf_runs (id INTEGER PRIMARY KEY)")
    row = DcfRunRow(
        ticker="GOOG",
        valuation_date=date(2026, 1, 1),
        horizon_years=5,
        wacc=0.1,
        npv=1.0,
        npv_per_share=1.0,
        shares_outstanding=1.0,
        currency="USD",
        live_price=None,
        live_price_at=None,
        mos_bar_used=None,
        assumption_snapshot_json="{}",
        provenance=DcfInputProvenance(
            input_sha256="a" * 64,
            workbook_sha256=None,
            engine_version="test",
            inputs_as_of=date(2026, 1, 1),
        ),
    )
    try:
        upsert(conn, row)
    except sqlite3.OperationalError as exc:
        assert "alembic upgrade head" in str(exc)
    else:  # pragma: no cover - test must fail if provenance can disappear
        raise AssertionError("provenance write was silently accepted")
